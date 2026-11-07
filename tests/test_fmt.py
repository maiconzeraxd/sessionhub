"""Testes dos formatadores compartilhados (fmt.py — CLI, TUI e preview)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sessionhub.fmt as fmt


def _iso(secs_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=secs_ago)).isoformat()


def test_age_buckets() -> None:
    assert fmt.age(None) == "—"
    assert fmt.age("lixo") == "?"
    assert fmt.age(_iso(10)) == "10s"
    assert fmt.age(_iso(5 * 60)) == "5min"
    assert fmt.age(_iso(3 * 3600)) == "3h"
    assert fmt.age(_iso(2 * 86400)) == "2d"
    assert fmt.age(_iso(3 * 7 * 86400)) == "3sem"


def test_age_futuro_e_ingenuo() -> None:
    # relógio levemente à frente → 0s, nunca negativo; naive → tratado como UTC
    assert fmt.age(_iso(-30)) == "0s"
    naive = (datetime.now(UTC) - timedelta(seconds=45)).replace(tzinfo=None)
    assert fmt.age(naive.isoformat()) == "45s"


def test_git_badges_row_de_sessao() -> None:
    row = {
        "wt_staged": 2,
        "wt_unstaged": 3,
        "wt_untracked": 7,
        "wt_ahead": 1,
        "wt_behind": 2,
        "wt_pr_number": 42,
    }
    assert fmt.git_badges(row) == "✎12 ↑1 ↓2 PR#42"


def test_git_badges_dict_de_worktree() -> None:
    wt = {"staged": 1, "unstaged": 0, "untracked": 2, "pr_number": 7}
    assert fmt.git_badges(wt) == "✎3 PR#7"


def test_git_badges_vazio() -> None:
    assert fmt.git_badges(None) == ""
    assert fmt.git_badges({}) == ""
    assert fmt.git_badges({"wt_staged": 0, "wt_unstaged": 0, "wt_untracked": 0}) == ""


def test_display_title_fallback_e_trunca() -> None:
    assert fmt.display_title({"title": "curto"}) == "curto"
    long_title = "x" * 80
    cut = fmt.display_title({"title": long_title})
    assert len(cut) == 58 and cut.endswith("…")  # 57 + elipse, cap 60
    assert fmt.display_title({"cwd": "/home/u/proj"}) == "(sem título) · proj"


def test_short_path_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fmt.Path, "home", lambda: Path("/home/u"))
    assert fmt.short_path("/home/u") == "~"
    assert fmt.short_path("/home/u/proj") == f"~{os.sep}proj"
    # fora da home e sem drive (POSIX) → nome do pai
    assert fmt.short_path("/opt/toolkit") == "opt"


def test_human_size() -> None:
    assert fmt.human_size(None) == "—"
    assert fmt.human_size(-5) == "—"
    assert fmt.human_size(0) == "0 B"
    assert fmt.human_size(512) == "512 B"
    assert fmt.human_size(2048) == "2,0 KB"
    assert fmt.human_size(12_000_000) == "11,4 MB"
    assert fmt.human_size(3_500_000_000) == "3,3 GB"


@pytest.mark.skipif(os.name != "nt", reason="drive de disco é coisa do Windows")
def test_short_path_drive_windows() -> None:
    # repos fora da home do usuário: G:\Dev\north → G:\…
    assert fmt.short_path(r"G:\Dev\north") == "G:\\…"
