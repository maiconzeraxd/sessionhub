"""Detecção de terminal/ambiente (usado pelo doctor e pelo `open`)."""

from __future__ import annotations

import os
import platform
import shutil

_CANDIDATES = {
    "tmux": "tmux",
    "kitty": "kitty",
    "wezterm": "wezterm",
    "alacritty": "alacritty",
    "gnome-terminal": "gnome-terminal",
    "konsole": "konsole",
    "x-terminal-emulator": "x-terminal-emulator",
    "wt": "wt.exe",
}


def current_terminal_program() -> str | None:
    """Terminal em que o SessionHub está rodando AGORA (nome curto).

    Prioriza o terminal atual na ordem de abertura: comandos `cli` (kitty @,
    wezterm cli) só funcionam de dentro do terminal certo. None = não dá
    para saber.
    """
    if os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if os.environ.get("WEZTERM_PANE"):
        return "wezterm"
    tp = (os.environ.get("TERM_PROGRAM") or "").lower()
    return {
        "kitty": "kitty",
        "ghostty": "ghostty",
        "wezterm": "wezterm",
        "tmux": "tmux",
        "iterm.app": "iterm",
        "apple_terminal": "apple-terminal",
    }.get(tp)


def detect() -> dict[str, object]:
    """Best-effort: terminal em uso + terminais disponíveis."""
    inside_tmux = bool(os.environ.get("TMUX"))
    inside_wt = bool(os.environ.get("WT_SESSION"))
    term_program = os.environ.get("TERM_PROGRAM") or ""
    available = {name: bool(shutil.which(bin_)) for name, bin_ in _CANDIDATES.items()}
    current: str | None = None
    if inside_tmux:
        current = "tmux"
    elif inside_wt:
        current = "wt"
    elif term_program:
        current = term_program
    return {
        "system": platform.system(),
        "current": current,
        "inside_tmux": inside_tmux,
        "inside_windows_terminal": inside_wt,
        "term_program": term_program or None,
        "available": available,
    }
