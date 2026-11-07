"""Testes da integração git (repo temporário real)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import git

import sessionhub.gitinfo as gitinfo
from sessionhub.gitinfo import (
    clear_pr_cache,
    collect_worktrees,
    parse_branch_header,
    parse_head_ts,
    parse_status,
    toplevel,
    worktree_list,
)


def _norm(path: Path | str) -> str:
    return os.path.normpath(str(path))


def test_toplevel(git_repo: Path) -> None:
    assert toplevel(git_repo) == _norm(git_repo)
    sub = git_repo / "sub"
    sub.mkdir()
    assert toplevel(sub) == _norm(git_repo)
    assert toplevel(git_repo.parent) is None  # fora do repo


def test_worktree_list(git_repo: Path, tmp_path: Path) -> None:
    wt = tmp_path / "wt1"
    git("worktree", "add", "-b", "wt1", str(wt), cwd=git_repo)
    wts = worktree_list(_norm(git_repo))
    assert len(wts) == 2
    branches = {w.branch for w in wts}
    assert branches == {"main", "wt1"}
    paths = {w.path for w in wts}
    assert _norm(git_repo) in paths and _norm(wt) in paths


def test_status_contagens(git_repo: Path) -> None:
    (git_repo / "README.md").write_text("modificado\n", encoding="utf-8")  # unstaged
    (git_repo / "novo.txt").write_text("x", encoding="utf-8")  # untracked
    novo = git_repo / "staged.txt"
    novo.write_text("y", encoding="utf-8")
    git("add", "staged.txt", cwd=git_repo)  # staged

    wts = collect_worktrees(_norm(git_repo), with_prs=False)
    main = [w for w in wts if w.path == _norm(git_repo)][0]
    assert main.staged == 1
    assert main.unstaged == 1
    assert main.untracked == 1
    assert main.branch == "main"
    assert main.ahead is None and main.behind is None  # sem upstream → None


def test_sem_upstream_ahead_behind_none(git_repo: Path) -> None:
    wts = collect_worktrees(_norm(git_repo), with_prs=False)
    assert wts[0].ahead is None and wts[0].behind is None


def test_ahead_behind_com_upstream(git_repo: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    git("clone", str(git_repo), str(clone), cwd=tmp_path)
    # commit novo no clone → ahead 1
    (clone / "README.md").write_text("clone edit\n", encoding="utf-8")
    git("add", "README.md", cwd=clone)
    git("commit", "-m", "clone edit", cwd=clone)
    wts = collect_worktrees(_norm(clone), with_prs=False)
    assert wts[0].ahead == 1 and wts[0].behind == 0
    # commit novo no "origin" → clone fica behind depois do fetch
    (git_repo / "origin.txt").write_text("o", encoding="utf-8")
    git("add", "origin.txt", cwd=git_repo)
    git("commit", "-m", "origin edit", cwd=git_repo)
    git("fetch", "origin", cwd=clone)
    wts = collect_worktrees(_norm(clone), with_prs=False)
    assert wts[0].ahead == 1 and wts[0].behind == 1


def test_worktree_detached(git_repo: Path, tmp_path: Path) -> None:
    wt = tmp_path / "det"
    git("worktree", "add", "--detach", str(wt), "HEAD", cwd=git_repo)
    wts = collect_worktrees(_norm(wt), with_prs=False)
    det = [w for w in wts if w.path == _norm(wt)]
    assert det, "worktree detached deveria estar na lista"
    assert det[0].branch is None


def test_parse_branch_header() -> None:
    assert parse_branch_header("## main") == ("main", None, None)
    assert parse_branch_header("## main...origin/main") == ("main", None, None)
    assert parse_branch_header("## main...origin/main [ahead 2]") == ("main", 2, 0)
    assert parse_branch_header("## main...origin/main [behind 3]") == ("main", 0, 3)
    assert parse_branch_header("## main...origin/main [ahead 2, behind 1]") == ("main", 2, 1)
    assert parse_branch_header("## HEAD (no branch)") == (None, None, None)
    assert parse_branch_header("## No commits yet on main") == ("main", None, None)


def test_parse_status() -> None:
    info = parse_status(
        "## main...origin/main [ahead 1]\n"
        " M mod.txt\n"
        "M  staged.txt\n"
        "?? novo.txt\n"
        "R  renomeado.txt -> novo-nome.txt\n"
    )
    assert info["branch"] == "main"
    assert info["ahead"] == 1 and info["behind"] == 0
    assert info["unstaged"] == 1
    assert info["staged"] == 2  # M + R
    assert info["untracked"] == 1


def test_parse_head_ts() -> None:
    # offset -03:00 → 13:00 UTC
    assert parse_head_ts("2026-11-01T10:00:00-03:00") == datetime(
        2026, 11, 1, 13, 0, tzinfo=UTC
    )
    assert parse_head_ts("2026-11-01T10:00:00+00:00") == datetime(
        2026, 11, 1, 10, 0, tzinfo=UTC
    )
    assert parse_head_ts("lixo") is None
    assert parse_head_ts("") is None


def test_collect_worktrees_preenche_head_ts(git_repo: Path) -> None:
    wts = collect_worktrees(_norm(git_repo), with_prs=False)
    assert wts, "repo de fixture deveria ter o worktree main"
    for w in wts:
        assert w.head_ts is not None
        assert w.head_ts.tzinfo is not None  # normalizado p/ UTC
    # worktree sem commits (repo recém-iniciado não é o caso: fixture commita)
    assert wts[0].head_ts is not None and wts[0].head_ts.year >= 2026


# ------------------------------------------------------------- cache de PRs


def test_pr_cache_evita_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duas chamadas seguidas → uma só ida ao gh (a 2ª vem do cache)."""
    calls: list[str] = []

    def fake_fetch(repo_root: str) -> dict[str, tuple[int, str]]:
        calls.append(repo_root)
        return {"main": (7, "https://x/pr/7")}

    monkeypatch.setattr(gitinfo, "_fetch_open_prs", fake_fetch)
    clear_pr_cache()
    first = gitinfo.repo_open_prs("C:/repo-a")
    second = gitinfo.repo_open_prs("C:/repo-a")
    assert first == second == {"main": (7, "https://x/pr/7")}
    assert calls == ["C:/repo-a"]
    # repos diferentes não compartilham entrada
    gitinfo.repo_open_prs("C:/repo-b")
    assert calls == ["C:/repo-a", "C:/repo-b"]


def test_pr_cache_expira_e_limpa(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_fetch(repo_root: str) -> dict[str, tuple[int, str]]:
        calls.append(repo_root)
        return {}

    monkeypatch.setattr(gitinfo, "_fetch_open_prs", fake_fetch)
    clear_pr_cache()
    # entrada com timestamp velho (TTL de 5min vencido) → refaz
    with gitinfo._PR_LOCK:
        gitinfo._PR_CACHE["/r"] = (time.monotonic() - 600.0, {})
    gitinfo.repo_open_prs("/r")
    assert calls == ["/r"]
    # clear_pr_cache zera → próxima chamada refaz
    clear_pr_cache()
    gitinfo.repo_open_prs("/r")
    assert calls == ["/r", "/r"]
