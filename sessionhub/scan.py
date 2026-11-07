"""Orquestra o scan: scanners → sessões; cwds → git → worktrees → índice."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .db import Database
from .gitinfo import clear_pr_cache, collect_worktrees, toplevel
from .log import get_logger
from .models import ScanStats, Worktree
from .scanners import available_scanners

log = get_logger(__name__)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def run_scan(
    db: Database,
    config: Config,
    full: bool = False,
    user_home: Path | None = None,
) -> ScanStats:
    """Uma passada completa de indexação.

    Ordem: scanners (incremental via fingerprints) → repos/worktrees
    (dos cwds das sessões novas + repos conhecidos + extra_repos) →
    enriquecimento das sessões → upsert → status derivado.
    """
    t0 = time.perf_counter()
    stats = ScanStats()
    if full:
        clear_pr_cache()  # --full também refaz as chamadas de PR
    scanners = available_scanners(user_home)
    stats.harnesses = [s.name for s in scanners]

    sessions = []
    seen_by_harness: dict[str, set[str]] = {}
    for scanner in scanners:
        fingerprints = None if full else db.get_fingerprints(scanner.name)
        result = scanner.discover(fingerprints)
        stats.sessions += len(result.sessions)
        stats.sessions_skipped += result.skipped
        stats.errors += result.errors
        sessions.extend(result.sessions)
        seen_by_harness[scanner.name] = result.seen

    # ---- repos/worktrees ------------------------------------------------
    cwd_to_wt: dict[str, str | None] = {}
    repos: dict[str, str] = {}
    for s in sessions:
        if not s.cwd:
            continue
        key = _norm(s.cwd)
        if key in cwd_to_wt:
            continue
        top = toplevel(s.cwd)
        cwd_to_wt[key] = top
        if top:
            repos[_norm(top)] = top
    # warm scan: repos já conhecidos continuam sendo atualizados
    for repo in db.known_repos():
        if repo and os.path.isdir(repo):
            norm = os.path.normpath(repo)
            repos[_norm(norm)] = norm
    for extra in config.extra_repos:
        p = os.path.expandvars(os.path.expanduser(extra))
        if os.path.isdir(p):
            top = toplevel(p)
            norm = top if top else os.path.normpath(p)
            repos[_norm(norm)] = norm

    stats.repos = len(repos)
    worktrees: dict[str, Worktree] = {}
    for repo in sorted(repos.values()):
        wts = collect_worktrees(repo)
        db.replace_worktrees(repo, wts)
        for wt in wts:
            worktrees[_norm(wt.path)] = wt
    stats.worktrees = len(worktrees)

    # ---- enriquece sessões com git -------------------------------------
    for s in sessions:
        if not s.cwd:
            continue
        wt_path = cwd_to_wt.get(_norm(s.cwd))
        if wt_path:
            s.worktree_path = wt_path
            info = worktrees.get(_norm(wt_path))
            if info is not None:
                s.repo_root = info.repo_root
                s.branch = info.branch or s.branch

    db.upsert_sessions(sessions)
    # sessões cujo transcript sumiu do disco → 'missing' (🗄); quem voltou é
    # restaurado. Importa para o backup (fase 4) e para não listar fantasmas.
    for harness, seen in seen_by_harness.items():
        db.mark_missing(harness, seen)
    db.set_meta("last_scan", datetime.now(UTC).isoformat())
    db.recompute_status(config.forgotten_hours)
    db.purge_events(config.retention_days)  # eventos velhos não acumulam

    stats.duration = time.perf_counter() - t0
    log.info(
        "scan: %d novas, %d inalteradas, %d repos, %d worktrees em %.2fs",
        stats.sessions,
        stats.sessions_skipped,
        stats.repos,
        stats.worktrees,
        stats.duration,
    )
    return stats
