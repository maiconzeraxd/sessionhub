"""Testes de integração do scan (fase 1, ponta a ponta)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from helpers import git, make_user_line, write_transcript

from sessionhub.config import Config
from sessionhub.db import Database
from sessionhub.scan import run_scan

SID = "11111111-2222-3333-4444-555555555555"


def _config(db: Database, tmp_path: Path) -> Config:
    home = tmp_path / "sh"
    return Config(home=home, db_path=db.path, log_dir=home / "logs", backup_dir=home / "backups")


def test_scan_end_to_end(fake_home: Path, git_repo: Path, db: Database, tmp_path: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(git_repo), "trabalha no repo")],
    )
    wt1 = tmp_path / "wt1"
    git("worktree", "add", "-b", "feat", str(wt1), cwd=git_repo)

    stats = run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert stats.sessions == 1
    assert stats.repos == 1
    assert stats.worktrees == 2  # main + wt1

    rows = db.list_sessions()
    assert rows[0]["repo_root"] == os.path.normpath(str(git_repo))
    assert rows[0]["worktree_path"] == os.path.normpath(str(git_repo))
    assert rows[0]["branch"] == "main"
    assert rows[0]["status"] == "stopped"
    # worktree sem sessão também aparece
    wt_paths = {w["path"] for w in db.list_worktrees()}
    assert os.path.normpath(str(wt1)) in wt_paths


def test_scan_worktree_esquecido(
    fake_home: Path, git_repo: Path, db: Database, tmp_path: Path
) -> None:
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(git_repo), "velha", ts=old)],
    )
    (git_repo / "dirty.txt").write_text("x", encoding="utf-8")  # untracked → sujo

    run_scan(db, _config(db, tmp_path), user_home=fake_home)
    rows = db.list_sessions()
    assert rows[0]["status"] == "forgotten"


def test_scan_cwd_sem_repo(fake_home: Path, db: Database, tmp_path: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "C--Users-x"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(tmp_path / "nao-repo"), "sem repo")],
    )
    stats = run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert stats.repos == 0
    rows = db.list_sessions()
    assert rows[0]["repo_root"] is None
    assert rows[0]["worktree_path"] is None
    assert rows[0]["status"] == "stopped"


def test_scan_cwd_inexistente_nao_quebra(fake_home: Path, db: Database, tmp_path: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--gone"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "Z:\\caminho\\sumiu", "fantasma")],
    )
    stats = run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert stats.sessions == 1
    assert stats.errors == 0
    assert db.list_sessions()[0]["worktree_path"] is None


def test_scan_incremental(fake_home: Path, git_repo: Path, db: Database, tmp_path: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, str(git_repo), "v1")])
    stats1 = run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert stats1.sessions == 1 and stats1.sessions_skipped == 0

    stats2 = run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert stats2.sessions == 0
    assert stats2.sessions_skipped == 1
    # índice continua com a sessão
    assert len(db.list_sessions()) == 1


def test_scan_extra_repos(fake_home: Path, git_repo: Path, db: Database, tmp_path: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--x"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(tmp_path / "nao-repo"), "x")],
    )
    cfg = _config(db, tmp_path)
    cfg.extra_repos = [str(git_repo)]
    stats = run_scan(db, cfg, user_home=fake_home)
    assert stats.repos == 1  # só o extra
    assert len(db.list_worktrees()) >= 1


def test_scan_purga_eventos_antigos(fake_home: Path, db: Database, tmp_path: Path) -> None:
    """O scan roda a retenção da tabela events (config.retention_days)."""
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, str(tmp_path), "v1")])
    db.record_event("2020-01-01T00:00:00+00:00", "claude", "velho", "Stop")
    db.record_event(datetime.now(UTC).isoformat(), "claude", "novo", "Stop")

    run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert db.last_events("claude", "velho") == []
    assert len(db.last_events("claude", "novo")) == 1


def test_scan_marca_missing_quando_transcript_sumiu(
    fake_home: Path, db: Database, tmp_path: Path
) -> None:
    projects = fake_home / ".claude" / "projects" / "G--repo"
    fp = write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, str(tmp_path), "v1")])
    run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert db.list_sessions()[0]["status"] == "stopped"

    fp.unlink()
    run_scan(db, _config(db, tmp_path), user_home=fake_home)
    rows = db.list_sessions()
    # a sessão continua no índice (não some) — só muda o status
    assert len(rows) == 1
    assert rows[0]["status"] == "missing"
    assert db.find_sessions(SID)[0]["status"] == "missing"

    # transcript voltou ao disco → status restaura
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, str(tmp_path), "v1")])
    run_scan(db, _config(db, tmp_path), user_home=fake_home)
    assert db.find_sessions(SID)[0]["status"] in ("stopped", "forgotten")
