"""Testes da abertura de sessão (montagem de comando; nada é executado)."""

from __future__ import annotations

import os
from typing import Any

from sessionhub.config import Config
from sessionhub.open import OpenResult, build_open_argv, open_in_editor, open_session


def _config(tmp_path: Any, **kwargs: Any) -> Config:
    home = tmp_path / "sh"
    defaults: dict[str, Any] = {
        "home": home,
        "db_path": home / "index.db",
        "log_dir": home / "logs",
        "backup_dir": home / "backups",
    }
    defaults.update(kwargs)
    return Config(**defaults)


def test_template_open_command(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command="minhaterm --cwd {cwd} -e {cmd}")
    argv, display = build_open_argv("G:\\dir", "claude --resume 123", cfg)
    assert argv == ["minhaterm", "--cwd", "G:\\dir", "-e", "claude", "--resume", "123"]
    assert display


def test_template_com_aspas(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command='minhaterm --cwd "{cwd}" -e "{cmd}"')
    argv, _ = build_open_argv("G:\\dir", "claude --resume 1", cfg)
    assert argv[:3] == ["minhaterm", "--cwd", "G:\\dir"]


def test_wt_no_windows(tmp_path: Any, monkeypatch: Any) -> None:

    if os.name != "nt":
        return
    monkeypatch.delenv("TMUX", raising=False)
    cfg = _config(tmp_path)  # terminal auto
    argv, _ = build_open_argv("G:\\dir", "claude --resume 1", cfg)
    assert argv is not None
    assert argv[0].lower().endswith("wt.exe") or "wt" in argv[0].lower()
    assert "-d" in argv and "G:\\dir" in argv


def test_tmux_quando_dentro_do_tmux(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,123,0")
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: "/usr/bin/tmux" if n == "tmux" else None
    )
    cfg = _config(tmp_path)
    argv, _ = build_open_argv("/home/eu/proj", "claude --resume 1", cfg)
    assert argv is not None
    assert argv[:3] == ["tmux", "new-window", "-c"]
    assert "/home/eu/proj" in argv
    assert "claude --resume 1" in argv


def test_open_session_dry_run(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command="echo {cwd} {cmd}")
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path),
        "cwd": str(tmp_path),
        "resume_command": "claude --resume 1",
    }
    result = open_session(row, cfg, dry_run=True)
    assert result.status == "dry_run"
    assert "claude --resume 1" in (result.command or "")


def test_open_session_caminho_sumiu(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command="echo {cwd} {cmd}")
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path / "sumiu"),
        "cwd": str(tmp_path / "sumiu"),
        "resume_command": "claude --resume 1",
    }
    result = open_session(row, cfg)
    assert isinstance(result, OpenResult)
    assert result.status == "missing"
    assert result.command == "claude --resume 1"


def test_open_session_sem_comando(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command="echo {cwd}")
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path),
        "cwd": str(tmp_path),
        "resume_command": "",
    }
    result = open_session(row, cfg)
    assert result.status == "error"


def test_open_session_repo_fallback_cwd_override(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command="echo {cwd} {cmd}")
    repo = tmp_path / "repo"
    repo.mkdir()
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path / "wt-sumiu"),
        "cwd": str(tmp_path / "wt-sumiu"),
        "repo_root": str(repo),
        "resume_command": "claude --resume 1",
    }
    result = open_session(row, cfg, cwd_override=str(repo), dry_run=True)
    assert result.status == "dry_run"
    assert str(repo) in (result.command or "")


def test_open_in_editor(tmp_path: Any, monkeypatch: Any) -> None:
    cfg = _config(tmp_path, editor="code")
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: "/usr/bin/code" if n == "code" else None
    )
    monkeypatch.setattr("sessionhub.open._launch", lambda argv: None)
    result = open_in_editor(str(tmp_path), cfg)  # type: ignore[arg-type]
    assert result.status == "opened"
