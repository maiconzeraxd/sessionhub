"""Testes da integração git (repo temporário real)."""

from __future__ import annotations

import os
from pathlib import Path

from helpers import git

from sessionhub.gitinfo import (
    collect_worktrees,
    parse_branch_header,
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
