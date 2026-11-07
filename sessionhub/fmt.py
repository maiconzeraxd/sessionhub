"""Formatadores compartilhados entre CLI e TUI (idade, badges git, título).

Antes duplicados em cli.py e tui.py — uma mudança aqui vale para os dois.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ------------------------------------------------------------- idade


def age(iso: str | None) -> str:
    """Timestamp ISO → idade legível (12s · 5min · 3h · 2d · 1sem · —)."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = max(0, int((datetime.now(UTC) - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}min"
    if secs < 86400:
        return f"{secs // 3600}h"
    if secs < 7 * 86400:
        return f"{secs // 86400}d"
    return f"{secs // (7 * 86400)}sem"


# ------------------------------------------------------------- git


def _get(data: Mapping[str, Any], *keys: str) -> Any:
    """Pega o primeiro campo existente (row de sessão usa wt_*, worktree não)."""
    for key in keys:
        if key in data:
            return data[key]
    return None


def git_badges(data: Mapping[str, Any] | None) -> str:
    """Badges de estado git: ✎12 · ↑3 · ↓1 · PR#42 (vazio = "")."""
    if not data:
        return ""
    parts: list[str] = []
    dirty = (
        int(_get(data, "wt_staged", "staged") or 0)
        + int(_get(data, "wt_unstaged", "unstaged") or 0)
        + int(_get(data, "wt_untracked", "untracked") or 0)
    )
    if dirty:
        parts.append(f"✎{dirty}")
    ahead = _get(data, "wt_ahead", "ahead")
    if ahead:
        parts.append(f"↑{ahead}")
    behind = _get(data, "wt_behind", "behind")
    if behind:
        parts.append(f"↓{behind}")
    pr = _get(data, "wt_pr_number", "pr_number")
    if pr:
        parts.append(f"PR#{pr}")
    return " ".join(parts)


def display_title(row: Mapping[str, Any], max_chars: int = 60) -> str:
    """Título da sessão com fallback e truncagem."""
    title = row.get("title")
    if not title:
        base = row.get("cwd") or "?"
        return f"(sem título) · {os.path.basename(base) or base}"
    return title if len(title) <= max_chars else title[: max_chars - 3] + "…"


# ------------------------------------------------------------- caminhos


def short_path(path: str) -> str:
    """Caminho abreviado para rótulos: home → ~, Windows → drive, resto → pai."""
    if not path:
        return "?"
    home = str(Path.home())
    norm = os.path.normpath(path)
    if norm == home:
        return "~"
    if norm.startswith(home + os.sep):
        rest = norm[len(home) + 1 :]
        return f"~{os.sep}{rest}"
    drive = os.path.splitdrive(norm)[0]
    if drive:
        return f"{drive}\\…"
    parent = os.path.basename(os.path.dirname(norm))
    return parent or norm
