"""Abre uma sessão num terminal novo (foca pane do tmux na fase 3).

Ordem de escolha do terminal:
1. `open_command` do config.toml (template com {cwd} e {cmd});
2. tmux, se estivermos dentro do tmux (ou config terminal = "tmux");
3. detecção por plataforma: Windows → wt.exe; macOS → iTerm2/Terminal/Ghostty/
   WezTerm/kitty; Linux → kitty/WezTerm/Alacritty/gnome-terminal/konsole.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import Config
from .log import get_logger
from .terminals import detect

log = get_logger(__name__)


@dataclass
class OpenResult:
    status: str  # "opened" | "missing" | "error" | "dry_run"
    command: str | None = None  # comando de retomada (ou argv montado, no dry_run)
    message: str = ""


def _split_template(rendered: str) -> list[str]:
    """Split respeitando aspas SEM comer barras invertidas do Windows."""
    tokens = shlex.split(rendered, posix=False)
    return [t.strip("\"'") for t in tokens]


def _sh_quote(path: str) -> str:
    return shlex.quote(path)


def build_open_argv(cwd: str, cmd: str, config: Config) -> tuple[list[str] | None, str]:
    """Monta o argv do terminal. Retorna (argv, comando legível).

    argv None = nenhum terminal encontrado (chamador decide o que fazer).
    """
    if config.open_command:
        argv = _split_template(config.open_command.replace("{cwd}", cwd).replace("{cmd}", cmd))
        return argv, " ".join(argv)

    term = (config.terminal or "auto").lower()
    inside_tmux = bool(os.environ.get("TMUX"))
    use_tmux = term == "tmux" or (term == "auto" and inside_tmux)
    if use_tmux and shutil.which("tmux"):
        argv = ["tmux", "new-window", "-c", cwd, cmd]
        return argv, " ".join(argv)

    system = os.name
    if system == "nt":
        if shutil.which("wt.exe"):
            argv = ["wt.exe", "-d", cwd, "cmd", "/k", cmd]
            return argv, " ".join(argv)
        return None, cmd

    # POSIX (macOS/Linux)
    cmd_tokens = shlex.split(cmd)
    if system == "posix" and shutil.which("wezterm"):
        argv = ["wezterm", "cli", "spawn", "--cwd", cwd, "--", *cmd_tokens]
        return argv, " ".join(argv)
    for binary, builder in _posix_candidates():
        if not shutil.which(binary):
            continue
        argv = builder(cwd, cmd, cmd_tokens)
        if argv:
            return argv, " ".join(argv)
    return None, cmd


def _posix_candidates() -> list[tuple[str, Any]]:
    def kitty_at(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["kitty", "@", "launch", "--type", "tab", "--cwd", cwd, *tokens]

    def kitty_new(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["kitty", "--directory", cwd, *tokens]

    def alacritty(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["alacritty", "-e", "sh", "-c", f"cd {_sh_quote(cwd)} && {cmd}"]

    def gnome(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["gnome-terminal", "--", "sh", "-c", f"cd {_sh_quote(cwd)} && {cmd}"]

    def konsole(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["konsole", "-e", "sh", "-c", f"cd {_sh_quote(cwd)} && {cmd}"]

    def xterm_default(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["x-terminal-emulator", "-e", "sh", "-c", f"cd {_sh_quote(cwd)} && {cmd}"]

    def osascript_terminal(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        script = f'tell application "Terminal" to do script "cd {_apple_quote(cwd)} && {cmd}"'
        return ["osascript", "-e", script]

    def osascript_iterm(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        script = (
            f'tell application "iTerm" to create window with default profile '
            f'command "cd {_apple_quote(cwd)} && {cmd}"'
        )
        return ["osascript", "-e", script]

    def ghostty(cwd: str, cmd: str, tokens: list[str]) -> list[str]:
        return ["ghostty", "--working-directory", cwd, "-e", *tokens]

    if detect().get("system") == "Darwin":
        return [
            ("kitty", kitty_at),
            ("ghostty", ghostty),
            ("wezterm", lambda c, m, t: ["wezterm", "cli", "spawn", "--cwd", c, "--", *t]),
            ("osascript", osascript_iterm),
            ("osascript", osascript_terminal),
        ]
    return [
        ("kitty", kitty_at),
        ("kitty", kitty_new),
        ("alacritty", alacritty),
        ("gnome-terminal", gnome),
        ("konsole", konsole),
        ("x-terminal-emulator", xterm_default),
    ]


def _apple_quote(path: str) -> str:
    return path.replace('"', '\\"')


def _launch(argv: list[str]) -> None:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    log.info("abriu terminal: %s", argv)


def open_session(
    row: dict[str, Any],
    config: Config,
    cwd_override: str | None = None,
    dry_run: bool = False,
) -> OpenResult:
    """Abre a sessão `row` num terminal novo no worktree dela.

    TODO fase 3: se a sessão está rodando num pane do tmux, focar o pane
    em vez de abrir outro (exige o mapa de processos ↔ panes).
    """
    cwd = cwd_override or row.get("worktree_path") or row.get("cwd")
    cmd = row.get("resume_command") or ""
    if not cwd or not os.path.isdir(cwd):
        return OpenResult("missing", cmd, f"caminho não existe mais: {cwd}")
    if not cmd:
        return OpenResult("error", None, "sessão sem comando de retomada")

    argv, display = build_open_argv(cwd, cmd, config)
    if argv is None:
        return OpenResult(
            "error", display, "nenhum terminal detectado — configure [terminal] ou open_command"
        )
    if dry_run:
        return OpenResult("dry_run", display, display)
    try:
        _launch(argv)
    except OSError as exc:
        return OpenResult("error", display, f"falha ao lançar: {exc}")
    return OpenResult("opened", display, f"{display}")


def open_in_editor(path: str, config: Config) -> OpenResult:
    """Abre o worktree no editor ($EDITOR ou code)."""
    if not os.path.isdir(path):
        return OpenResult("missing", None, f"caminho não existe: {path}")
    editor = config.editor or "code"
    if shutil.which(editor) or os.path.isfile(editor):
        argv = [editor, path]
    else:
        argv = _split_template(editor) + [path]
    if not any(shutil.which(a) or os.path.isfile(a) for a in argv[:1]):
        return OpenResult("error", None, f"editor não encontrado: {editor}")
    try:
        _launch(argv)
    except OSError as exc:
        return OpenResult("error", " ".join(argv), f"falha ao lançar editor: {exc}")
    return OpenResult("opened", " ".join(argv), " ".join(argv))
