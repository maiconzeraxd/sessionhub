"""Integração git: repositórios, worktrees e status (somente leitura)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .log import get_logger
from .models import Worktree

log = get_logger(__name__)

GIT_TIMEOUT = 20.0
_MAX_WORKERS = 8

# gh pr list é uma chamada de rede (~1s+ por repo): a TUI consulta a cada
# rebuild, então cacheamos por repo com TTL de 5 minutos.
_PR_TTL = 300.0
_PR_CACHE: dict[str, tuple[float, dict[str, tuple[int, str]]]] = {}
_PR_LOCK = threading.Lock()


def parse_head_ts(raw: str) -> datetime | None:
    """`git log -1 --format=%cI` (ISO 8601 com offset) → datetime UTC."""
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class GitStatus(TypedDict):
    """Resultado de `parse_status`."""

    branch: str | None
    ahead: int | None
    behind: int | None
    staged: int
    unstaged: int
    untracked: int


def run_git(
    args: list[str], cwd: str | Path | None = None, timeout: float = GIT_TIMEOUT
) -> str | None:
    """Roda git e devolve stdout; None em qualquer falha (silencioso)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("git %s falhou em %s: %s", args[:2], cwd, exc)
        return None
    if proc.returncode != 0:
        log.debug("git %s rc=%s: %s", args[:2], proc.returncode, (proc.stderr or "")[:200])
        return None
    return proc.stdout


def toplevel(path: str | Path) -> str | None:
    """Repo/worktree raiz de um diretório (None se não for repo ou não existir)."""
    if not os.path.isdir(path):
        return None
    out = run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if not out:
        return None
    return os.path.normpath(out.strip())


def worktree_list(repo_root: str) -> list[Worktree]:
    """`git worktree list --porcelain` → worktrees do repo (sem status)."""
    out = run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if out is None:
        return []
    worktrees: list[Worktree] = []
    path: str | None = None
    head: str | None = None
    branch_ref: str | None = None

    def flush() -> None:
        nonlocal path, head, branch_ref
        if path:
            branch = branch_ref[len("refs/heads/") :] if branch_ref else None
            worktrees.append(
                Worktree(repo_root=repo_root, path=os.path.normpath(path), head=head, branch=branch)
            )
        path, head, branch_ref = None, None, None

    for line in out.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            path = value
        elif key == "HEAD":
            head = value
        elif key == "branch":
            branch_ref = value
        elif key == "bare":
            flush()
    flush()
    return worktrees


def parse_branch_header(line: str) -> tuple[str | None, int | None, int | None]:
    """Linha `## branch...origin/branch [ahead 2, behind 1]` → (branch, ahead, behind)."""
    rest = line[3:] if line.startswith("## ") else line
    ahead: int | None = None
    behind: int | None = None
    m = re.search(r"\[([^\]]*)\]\s*$", rest)
    if m:
        bracket = m.group(1)
        rest = rest[: m.start()].strip()
        ma = re.search(r"ahead (\d+)", bracket)
        mb = re.search(r"behind (\d+)", bracket)
        if ma or mb:
            ahead = int(ma.group(1)) if ma else 0
            behind = int(mb.group(1)) if mb else 0
    if rest.startswith("HEAD (no branch)"):
        return None, ahead, behind
    if rest.startswith("No commits yet on "):
        return rest[len("No commits yet on ") :].strip(), ahead, behind
    branch = rest.split("...")[0].strip()
    return (branch or None), ahead, behind


def parse_status(out: str) -> GitStatus:
    """Saída de `git status --porcelain=v1 --branch` → contagens + branch."""
    lines = out.splitlines()
    branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    staged = unstaged = untracked = 0
    if lines and lines[0].startswith("##"):
        branch, ahead, behind = parse_branch_header(lines[0])
    for line in lines[1:]:
        if len(line) < 4:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            untracked += 1
        elif x == "!" and y == "!":
            continue  # ignored
        else:
            if x not in (" ", "?"):
                staged += 1
            if y not in (" ", "?"):
                unstaged += 1
    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
    }


def _fetch_open_prs(repo_root: str) -> dict[str, tuple[int, str]]:
    """Chama `gh pr list` de verdade (sem cache)."""
    if shutil.which("gh") is None:
        return {}
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,url,headRefName",
                "--limit",
                "300",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    out: dict[str, tuple[int, str]] = {}
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            head, num, url = it.get("headRefName"), it.get("number"), it.get("url")
            if head and num and url:
                out[str(head)] = (int(num), str(url))
    return out


def clear_pr_cache() -> None:
    """Zera o cache de PRs (testes; scan --full pode chamar antes)."""
    with _PR_LOCK:
        _PR_CACHE.clear()


def repo_open_prs(repo_root: str, ttl: float = _PR_TTL) -> dict[str, tuple[int, str]]:
    """PRs abertos por branch, via gh (vazio se gh indisponível/sem auth).

    Cacheado por repo com TTL (5min por padrão) — cada rebuild da TUI não
    refaz a chamada de rede. Falhas também são cacheadas (evita repetir um
    timeout de 30s a cada scan).
    """
    now = time.monotonic()
    with _PR_LOCK:
        hit = _PR_CACHE.get(repo_root)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    prs = _fetch_open_prs(repo_root)
    with _PR_LOCK:
        _PR_CACHE[repo_root] = (now, prs)
    return prs


def collect_worktrees(
    repo_root: str, with_status: bool = True, with_prs: bool = True
) -> list[Worktree]:
    """Worktrees do repo com status (branch, dirty, ahead/behind) e PR aberto.

    As chamadas de status rodam em paralelo (cada uma é um subprocesso).
    """
    worktrees = [w for w in worktree_list(repo_root) if os.path.isdir(w.path)]

    if with_status:

        def _fill_status(w: Worktree) -> None:
            out = run_git(["status", "--porcelain=v1", "--branch"], cwd=w.path)
            if out is not None:
                info = parse_status(out)
                w.branch = info["branch"] or w.branch
                w.staged = info["staged"]
                w.unstaged = info["unstaged"]
                w.untracked = info["untracked"]
                w.ahead = info["ahead"]
                w.behind = info["behind"]
            log_out = run_git(["log", "-1", "--format=%cI"], cwd=w.path)
            if log_out:
                w.head_ts = parse_head_ts(log_out)

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            list(pool.map(_fill_status, worktrees))

    if with_prs:
        prs = repo_open_prs(repo_root)
        for w in worktrees:
            pr = prs.get(w.branch or "")
            if pr:
                w.pr_number, w.pr_url = pr
    return worktrees
