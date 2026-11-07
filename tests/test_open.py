"""Testes da abertura de sessão (montagem de comando; nada é executado)."""

from __future__ import annotations

import os
from typing import Any

from sessionhub.config import Config
from sessionhub.open import (
    Launcher,
    OpenResult,
    _osascript_argv,
    build_launchers,
    build_open_argv,
    open_in_editor,
    open_session,
)


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


def _clear_term_env(monkeypatch: Any) -> None:
    for var in ("TMUX", "TERM_PROGRAM", "KITTY_WINDOW_ID", "WEZTERM_PANE", "WT_SESSION"):
        monkeypatch.delenv(var, raising=False)


def test_template_open_command(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command="minhaterm --cwd {cwd} -e {cmd}")
    argv, display = build_open_argv("G:\\dir", "claude --resume 123", cfg)
    # {cmd} é UM argumento (o comando inteiro), não palavras soltas
    assert argv == ["minhaterm", "--cwd", "G:\\dir", "-e", "claude --resume 123"]
    assert display


def test_template_com_aspas(tmp_path: Any) -> None:
    cfg = _config(tmp_path, open_command='minhaterm --cwd "{cwd}" -e "{cmd}"')
    argv, _ = build_open_argv("G:\\dir", "claude --resume 1", cfg)
    assert argv[:3] == ["minhaterm", "--cwd", "G:\\dir"]


def test_template_divide_antes_de_substituir(tmp_path: Any) -> None:
    """Caminho com espaço: substituir DEPOIS do split mantém um argumento só."""
    cfg = _config(tmp_path, open_command="minhaterm --cwd {cwd} -e {cmd}")
    argv, _ = build_open_argv("G:\\my dir", "claude --resume 1", cfg)
    assert argv == ["minhaterm", "--cwd", "G:\\my dir", "-e", "claude --resume 1"]
    assert "G:\\my dir" in argv  # um token, não dois


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


def test_dentro_do_kitty_usa_kitty_at(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Dentro do kitty: prioridade para `kitty @ launch` (verificável)."""
    _clear_term_env(monkeypatch)
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: "/usr/bin/kitty" if n == "kitty" else None
    )
    cfg = _config(tmp_path)
    launchers = build_launchers("/home/eu/proj", "claude --resume 1", cfg)
    assert launchers, "deveria ter launcher do kitty"
    first = launchers[0]
    assert first.kind == "cli"
    assert first.argv[:3] == ["kitty", "@", "launch"]
    assert "--cwd" in first.argv and "/home/eu/proj" in first.argv


def test_dentro_do_wezterm_usa_cli_spawn(tmp_path: Any, monkeypatch: Any) -> None:
    _clear_term_env(monkeypatch)
    monkeypatch.setenv("WEZTERM_PANE", "pane-id")
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: "/usr/bin/wezterm" if n == "wezterm" else None
    )
    cfg = _config(tmp_path)
    launchers = build_launchers("/home/eu/proj", "claude --resume 1", cfg)
    assert launchers and launchers[0].kind == "cli"
    assert launchers[0].argv[:3] == ["wezterm", "cli", "spawn"]


def test_fora_do_kitty_abre_instancia_nova(tmp_path: Any, monkeypatch: Any) -> None:
    _clear_term_env(monkeypatch)
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: "/usr/bin/kitty" if n == "kitty" else None
    )
    # simula os defaults POSIX (kitty como candidato da plataforma)
    monkeypatch.setattr(
        "sessionhub.open._platform_defaults", lambda: ["kitty", "wezterm", "alacritty"]
    )
    cfg = _config(tmp_path)
    launchers = build_launchers("/home/eu/proj", "claude --resume 1", cfg)
    assert launchers and launchers[0].kind == "spawn"
    assert launchers[0].argv[0] == "kitty"
    assert "--directory" in launchers[0].argv


def test_terminal_do_config_e_respeitado(tmp_path: Any, monkeypatch: Any) -> None:
    """config terminal="kitty" (explícito) ganha de tudo — mesmo fora do kitty."""
    _clear_term_env(monkeypatch)
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: f"/usr/bin/{n}" if n == "kitty" else None
    )
    cfg = _config(tmp_path, terminal="kitty")
    launchers = build_launchers("/p", "claude --resume 1", cfg)
    assert launchers and launchers[0].argv[0] == "kitty"


def test_terminal_do_config_alias_wt(tmp_path: Any, monkeypatch: Any) -> None:
    if os.name != "nt":
        return
    _clear_term_env(monkeypatch)
    monkeypatch.setattr(
        "sessionhub.open.shutil.which", lambda n: "C:/wt.exe" if n == "wt.exe" else None
    )
    cfg = _config(tmp_path, terminal="wt")
    launchers = build_launchers("C:\\p", "claude --resume 1", cfg)
    assert launchers and launchers[0].argv[0] == "wt.exe"


def test_osascript_escapa_duas_camadas() -> None:
    """AppleScript: literal escapado (\\ e ") em volta de um comando sh -c."""
    argv = _osascript_argv("Terminal", "do script", '/home/eu/we"ird dir', "claude --resume 1")
    assert argv[:2] == ["osascript", "-e"]
    script = argv[2]
    assert script.startswith('tell application "Terminal" to do script "')
    assert "sh -c " in script  # sh -c explícito: funciona com ou sem shell do app
    assert "claude --resume 1" in script
    assert '\\"' in script  # aspas escapadas para o AppleScript


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


def test_open_session_dry_run_sem_terminal(tmp_path: Any, monkeypatch: Any) -> None:
    """--dry-run sem nenhum terminal instalado NÃO é erro (bug 8)."""
    _clear_term_env(monkeypatch)
    monkeypatch.setattr("sessionhub.open.shutil.which", lambda n: None)
    cfg = _config(tmp_path)  # terminal auto, sem open_command
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path),
        "cwd": str(tmp_path),
        "resume_command": "claude --resume 1",
    }
    result = open_session(row, cfg, dry_run=True)
    assert result.status == "dry_run"
    assert result.command == "claude --resume 1"


def test_open_session_id_suspeito_recusa_executar(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """session_id com metacaracter de shell não vai para cmd/sh/tmux."""
    cfg = _config(tmp_path, open_command="echo {cwd} {cmd}")
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path),
        "cwd": str(tmp_path),
        "session_id": "abc; rm -rf ~",
        "resume_command": "claude --resume abc; rm -rf ~",
    }
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "sessionhub.open.run_launcher",
        lambda launcher, timeout=15.0: launched.append(launcher.argv) or True,
    )
    result = open_session(row, cfg)
    assert result.status == "error"
    assert "suspeito" in result.message
    assert launched == []


def test_open_session_id_valido_abre(tmp_path: Any, monkeypatch: Any) -> None:
    cfg = _config(tmp_path, open_command="echo {cwd} {cmd}")
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path),
        "cwd": str(tmp_path),
        "session_id": "11111111-2222-3333-4444-555555555555",
        "resume_command": "claude --resume 11111111-2222-3333-4444-555555555555",
    }
    monkeypatch.setattr("sessionhub.open.run_launcher", lambda *a, **kw: True)
    result = open_session(row, cfg)
    assert result.status == "opened"


def test_open_session_falha_de_cli_tenta_proximo(tmp_path: Any, monkeypatch: Any) -> None:
    """Launcher cli que falha (rc != 0) cai para o próximo da lista."""
    _clear_term_env(monkeypatch)
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.setattr(
        "sessionhub.open.shutil.which",
        lambda n: f"/usr/bin/{n}" if n in ("kitty", "tmux") else None,
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,123,0")
    cfg = _config(tmp_path)
    calls: list[str] = []

    def fake_run(launcher: Launcher, timeout: float = 15.0) -> bool:
        calls.append(launcher.label)
        return launcher.label != "kitty @"

    monkeypatch.setattr("sessionhub.open.run_launcher", fake_run)
    row: dict[str, Any] = {
        "worktree_path": str(tmp_path),
        "cwd": str(tmp_path),
        "session_id": "11111111-2222-3333-4444-555555555555",
        "resume_command": "claude --resume 11111111-2222-3333-4444-555555555555",
    }
    result = open_session(row, cfg)
    assert result.status == "opened"
    assert calls == ["kitty @", "tmux"]  # falhou no kitty @, abriu no tmux


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
