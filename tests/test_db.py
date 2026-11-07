"""Testes do índice SQLite."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sessionhub.db import Database
from sessionhub.models import Session, Worktree


def _session(**kwargs: object) -> Session:
    base = dict(
        harness="claude",
        session_id="aaaa1111",
        title="t1",
        cwd="G:\\x",
        last_activity=datetime(2026, 11, 1, tzinfo=UTC),
        message_count=3,
        transcript_path="G:\\t.jsonl",
        transcript_mtime=1000.5,
        transcript_size=10,
        status="stopped",
    )
    base.update(kwargs)
    return Session(**base)  # type: ignore[arg-type]


def test_upsert_preserva_campos_do_usuario(db: Database) -> None:
    db.upsert_sessions([_session()])
    db.set_note("claude", "aaaa1111", "minha nota", "urgente")
    db.upsert_sessions([_session(title="t2")])
    rows = db.list_sessions()
    assert len(rows) == 1
    assert rows[0]["title"] == "t2"
    assert rows[0]["note"] == "minha nota"
    assert rows[0]["tags"] == "urgente"


def test_find_sessions_por_prefixo(db: Database) -> None:
    db.upsert_sessions([_session(session_id="aaaa1111"), _session(session_id="bbbb2222")])
    assert len(db.find_sessions("aaaa")) == 1
    assert len(db.find_sessions("bbbb2222")) == 1
    assert len(db.find_sessions("")) == 2  # prefixo vazio casa com tudo
    assert db.find_sessions("zzzz") == []


def test_status_counts_e_listagem(db: Database) -> None:
    db.upsert_sessions(
        [
            _session(session_id="1", status="waiting"),
            _session(session_id="2", status="waiting"),
            _session(session_id="3", status="forgotten"),
        ]
    )
    assert db.status_counts() == {"waiting": 2, "forgotten": 1}
    assert len(db.list_sessions(status="waiting")) == 2
    assert len(db.list_sessions(limit=2)) == 2
    assert len(db.list_sessions(include_hidden=True)) == 3


def test_hidden_esconde_da_listagem(db: Database) -> None:
    db.upsert_sessions([_session(session_id="1"), _session(session_id="2")])
    db.set_hidden("claude", "1", True)
    assert len(db.list_sessions()) == 1
    assert len(db.list_sessions(include_hidden=True)) == 2
    db.set_hidden("claude", "1", False)
    assert len(db.list_sessions()) == 2


def test_replace_worktrees(db: Database) -> None:
    db.replace_worktrees(
        "G:\\repo",
        [Worktree(repo_root="G:\\repo", path="G:\\repo", branch="main", staged=1, untracked=2)],
    )
    assert len(db.list_worktrees()) == 1
    db.replace_worktrees(
        "G:\\repo",
        [Worktree(repo_root="G:\\repo", path="G:\\repo\\wt1", branch="wt1")],
    )
    wts = db.list_worktrees()
    assert len(wts) == 1 and wts[0]["path"] == "G:\\repo\\wt1"
    # join com sessões
    db.upsert_sessions([_session(worktree_path="G:\\repo\\wt1")])
    rows = db.list_sessions()
    assert rows[0]["branch"] == "wt1"
    assert rows[0]["wt_staged"] == 0


def test_recompute_status_forgotten(db: Database) -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    fresh = datetime.now(UTC)
    db.replace_worktrees(
        "G:\\repo",
        [
            Worktree(repo_root="G:\\repo", path="G:\\repo\\sujo", unstaged=3),
            Worktree(repo_root="G:\\repo", path="G:\\repo\\limpo"),
        ],
    )
    db.upsert_sessions(
        [
            _session(session_id="old", worktree_path="G:\\repo\\sujo", last_activity=old),
            _session(session_id="new", worktree_path="G:\\repo\\sujo", last_activity=fresh),
            _session(session_id="limpo", worktree_path="G:\\repo\\limpo", last_activity=old),
        ]
    )
    db.recompute_status(forgotten_hours=24)
    by_id = {r["session_id"]: r["status"] for r in db.list_sessions(include_hidden=True)}
    assert by_id["old"] == "forgotten"
    assert by_id["new"] == "stopped"
    assert by_id["limpo"] == "stopped"


def test_recompute_status_ahead_conta_como_trabalho(db: Database) -> None:
    """Worktree limpa mas com commits não enviados (ahead) também esquece."""
    old = datetime(2020, 1, 1, tzinfo=UTC)
    db.replace_worktrees(
        "G:\\repo", [Worktree(repo_root="G:\\repo", path="G:\\repo", ahead=2)]
    )
    db.upsert_sessions([_session(session_id="s", worktree_path="G:\\repo", last_activity=old)])
    db.recompute_status(forgotten_hours=24)
    assert db.list_sessions()[0]["status"] == "forgotten"


def test_recompute_worktree_esquecida_sem_sessao(db: Database) -> None:
    """Worktree sem nenhuma sessão recente, suja e parada no tempo → 🔴."""
    old = datetime(2020, 1, 1, tzinfo=UTC)
    fresh = datetime.now(UTC)
    db.replace_worktrees(
        "G:\\repo",
        [
            Worktree(repo_root="G:\\repo", path="G:\\orfa", unstaged=1, head_ts=old),
            Worktree(repo_root="G:\\repo", path="G:\\orfa-limpa", head_ts=old),
            Worktree(repo_root="G:\\repo", path="G:\\orfa-nova", unstaged=1, head_ts=fresh),
            Worktree(repo_root="G:\\repo", path="G:\\com-sessao", unstaged=1, head_ts=old),
        ],
    )
    db.upsert_sessions(
        [_session(session_id="s", worktree_path="G:\\com-sessao", last_activity=fresh)]
    )
    db.recompute_status(forgotten_hours=24)
    by_path = {w["path"]: w["forgotten"] for w in db.list_worktrees()}
    assert by_path["G:\\orfa"] == 1  # suja + head velho + sem sessão
    assert by_path["G:\\orfa-limpa"] == 0  # limpa: nada a perder
    assert by_path["G:\\orfa-nova"] == 0  # commit recente: trabalho em curso
    assert by_path["G:\\com-sessao"] == 0  # sessão recente nela manda


def test_replace_worktrees_persiste_head_ts(db: Database) -> None:
    ts = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    db.replace_worktrees("G:\\r", [Worktree(repo_root="G:\\r", path="G:\\r", head_ts=ts)])
    assert db.list_worktrees()[0]["head_ts"] == "2026-11-01T12:00:00+00:00"


def test_migracao_v1_para_v2(tmp_path: Path) -> None:
    """Banco criado antes das colunas head_ts/forgotten é migrado ao abrir."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE worktrees (
            path        TEXT PRIMARY KEY,
            repo_root   TEXT NOT NULL,
            branch      TEXT,
            head        TEXT,
            staged      INTEGER NOT NULL DEFAULT 0,
            unstaged    INTEGER NOT NULL DEFAULT 0,
            untracked   INTEGER NOT NULL DEFAULT 0,
            ahead       INTEGER,
            behind      INTEGER,
            pr_number   INTEGER,
            pr_url      TEXT,
            scanned_at  TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO worktrees (path, repo_root, branch, unstaged) VALUES (?,?,?,?)",
        ("G:\\wt", "G:\\repo", "main", 2),
    )
    conn.commit()
    conn.close()

    db = Database(path)  # __init__ roda as migrações
    try:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(worktrees)")}
        assert "head_ts" in cols
        assert "forgotten" in cols
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # dado pré-existente preservado
        wts = db.list_worktrees()
        assert wts[0]["branch"] == "main"
        assert wts[0]["unstaged"] == 2
        assert wts[0]["head_ts"] is None
        assert wts[0]["forgotten"] == 0
    finally:
        db.close()

    # reabrir: idempotente (user_version já é 2)
    again = Database(path)
    again.close()


def test_banco_novo_nasce_na_versao_atual(db: Database) -> None:
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 2


def test_fingerprints(db: Database) -> None:
    db.upsert_sessions([_session()])
    fps = db.get_fingerprints("claude")
    assert fps["G:\\t.jsonl"] == (1000.5, 10)
    assert db.get_fingerprints("codex") == {}


def test_known_repos(db: Database) -> None:
    db.replace_worktrees("G:\\repo", [Worktree(repo_root="G:\\repo", path="G:\\repo")])
    db.upsert_sessions([_session(repo_root="G:\\outro")])
    assert set(db.known_repos()) == {"G:\\repo", "G:\\outro"}


def test_eventos(db: Database) -> None:
    db.record_event("2026-11-01T10:00:00Z", "claude", "s1", "Notification")
    db.record_event("2026-11-01T10:01:00Z", "claude", "s1", "Stop")
    events = db.last_events("claude", "s1")
    assert len(events) == 2
    assert events[0]["event"] == "Stop"  # mais recente primeiro


def test_purge_events_por_idade(db: Database) -> None:
    db.record_event("2026-01-01T00:00:00+00:00", "claude", "velho", "Notification")
    db.record_event("2030-01-01T00:00:00+00:00", "claude", "futuro", "Stop")
    assert db.purge_events(30) == 1  # o de janeiro sai; o do futuro fica
    assert db.last_events("claude", "velho") == []
    assert len(db.last_events("claude", "futuro")) == 1
    assert db.purge_events(30) == 0  # idempotente
    # evento de 10 dias atrás: sobrevive ao purge(30), sai no purge(0)
    db.record_event(
        (datetime.now(UTC) - timedelta(days=10)).isoformat(), "claude", "recente", "Stop"
    )
    assert db.purge_events(0) == 1  # 0 dias = tudo até agora
    assert db.last_events("claude", "recente") == []


def test_mark_missing_e_restaura(db: Database) -> None:
    db.upsert_sessions(
        [
            _session(session_id="1", transcript_path="G:\\a.jsonl"),
            _session(session_id="2", transcript_path="G:\\b.jsonl"),
            _session(session_id="3", harness="codex", transcript_path="C:\\c.jsonl"),
        ]
    )
    # transcript de "2" sumiu do disco
    n = db.mark_missing("claude", {"G:\\a.jsonl"})
    assert n == 1
    by_id = {
        (r["harness"], r["session_id"]): r["status"]
        for r in db.list_sessions(include_hidden=True)
    }
    assert by_id[("claude", "1")] == "stopped"
    assert by_id[("claude", "2")] == "missing"
    assert by_id[("codex", "3")] == "stopped"  # outro harness intocado

    # repetir o mesmo seen: idempotente (nada a marcar)
    assert db.mark_missing("claude", {"G:\\a.jsonl"}) == 0

    # arquivo de "2" voltou → restaura para stopped
    db.mark_missing("claude", {"G:\\a.jsonl", "G:\\b.jsonl"})
    by_id = {
        (r["harness"], r["session_id"]): r["status"]
        for r in db.list_sessions(include_hidden=True)
    }
    assert by_id[("claude", "2")] == "stopped"

    # todos os transcripts sumiram
    assert db.mark_missing("claude", set()) == 2
    by_id = {
        (r["harness"], r["session_id"]): r["status"]
        for r in db.list_sessions(include_hidden=True)
    }
    assert by_id[("claude", "1")] == "missing"
    assert by_id[("codex", "3")] == "stopped"
