"""Performance: scan incremental de ~2.000 sessões em < 1s (índice quente)."""

from __future__ import annotations

import time
from pathlib import Path

from helpers import make_user_line, write_transcript

from sessionhub.config import Config
from sessionhub.db import Database
from sessionhub.scan import run_scan


def test_warm_scan_2000_sessoes_menor_que_1s(fake_home: Path, db: Database, tmp_path: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "perf"
    cwd_gone = str(tmp_path / "nao-existe")  # sem git → zero chamadas de git
    for i in range(2000):
        sid = f"{i:08d}-1111-2222-3333-444444444444"
        write_transcript(
            projects / f"{sid}.jsonl",
            [make_user_line(sid, cwd_gone, f"sessão sintética número {i}")],
        )

    home = tmp_path / "sh"
    config = Config(home=home, db_path=db.path, log_dir=home / "logs", backup_dir=home / "backups")

    stats1 = run_scan(db, config, user_home=fake_home)
    assert stats1.sessions == 2000

    t0 = time.perf_counter()
    stats2 = run_scan(db, config, user_home=fake_home)
    warm = time.perf_counter() - t0
    assert stats2.sessions == 0
    assert stats2.sessions_skipped == 2000
    assert len(db.list_sessions(limit=0)) == 2000
    assert warm < 1.0, f"warm scan levou {warm:.2f}s (>1s)"
