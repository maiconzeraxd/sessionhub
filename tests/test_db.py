"""Testes do índice SQLite."""

from __future__ import annotations

from datetime import UTC, datetime

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
