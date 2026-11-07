"""Índice SQLite do SessionHub (~/.sessionhub/index.db)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .log import get_logger
from .models import Session, Worktree

log = get_logger(__name__)

_SELECT_SESSIONS = """
SELECT s.harness, s.session_id, s.title, s.cwd, s.repo_root, s.worktree_path,
       COALESCE(w.branch, s.branch) AS branch,
       s.created_at, s.last_activity, s.message_count, s.transcript_path,
       s.resume_command, s.status, s.hidden, s.note, s.tags,
       w.staged AS wt_staged, w.unstaged AS wt_unstaged, w.untracked AS wt_untracked,
       w.ahead AS wt_ahead, w.behind AS wt_behind,
       w.pr_number AS wt_pr_number, w.pr_url AS wt_pr_url,
       CASE WHEN w.path IS NOT NULL THEN 1 ELSE 0 END AS worktree_known
FROM sessions s
LEFT JOIN worktrees w ON w.path = s.worktree_path
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    harness          TEXT    NOT NULL,
    session_id       TEXT    NOT NULL,
    title            TEXT,
    cwd              TEXT,
    repo_root        TEXT,
    worktree_path    TEXT,
    branch           TEXT,
    created_at       TEXT,
    last_activity    TEXT,
    message_count    INTEGER NOT NULL DEFAULT 0,
    transcript_path  TEXT,
    resume_command   TEXT,
    status           TEXT    NOT NULL DEFAULT 'unknown',
    transcript_mtime REAL,
    transcript_size  INTEGER,
    hidden           INTEGER NOT NULL DEFAULT 0,
    note             TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '',
    scanned_at       TEXT,
    PRIMARY KEY (harness, session_id)
);
CREATE INDEX IF NOT EXISTS ix_sessions_last_activity ON sessions(last_activity DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_worktree ON sessions(worktree_path);
CREATE INDEX IF NOT EXISTS ix_sessions_transcript ON sessions(transcript_path);
CREATE INDEX IF NOT EXISTS ix_sessions_harness ON sessions(harness);

CREATE TABLE IF NOT EXISTS worktrees (
    path        TEXT PRIMARY KEY,
    repo_root   TEXT NOT NULL,
    branch      TEXT,
    head        TEXT,
    staged      INTEGER NOT NULL DEFAULT 0,
    unstaged    INTEGER NOT NULL DEFAULT 0,
    untracked   INTEGER NOT NULL DEFAULT 0,
    ahead       INTEGER,
    behind      INTEGER,
    pr_number   INTEGER,
    pr_url      TEXT,
    scanned_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_worktrees_repo ON worktrees(repo_root);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    harness         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    event           TEXT NOT NULL,
    cwd             TEXT,
    transcript_path TEXT,
    payload         TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_recent ON events(ts DESC);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


class Database:
    """Acesso ao índice. Uma instância = uma conexão (single-thread)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- sessões

    def upsert_sessions(self, sessions: Iterable[Session]) -> int:
        """Insere/atualiza sessões. note/tags/hidden do usuário NÃO são tocados."""
        now = _iso(datetime.now(UTC))
        rows = []
        for s in sessions:
            rows.append(
                (
                    s.harness,
                    s.session_id,
                    s.title,
                    s.cwd,
                    s.repo_root,
                    s.worktree_path,
                    s.branch,
                    _iso(s.created_at),
                    _iso(s.last_activity),
                    s.message_count,
                    s.transcript_path,
                    s.resume_command,
                    s.status,
                    s.transcript_mtime,
                    s.transcript_size,
                    now,
                )
            )
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO sessions (harness, session_id, title, cwd, repo_root,
                    worktree_path, branch, created_at, last_activity, message_count,
                    transcript_path, resume_command, status, transcript_mtime,
                    transcript_size, scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(harness, session_id) DO UPDATE SET
                    title=excluded.title, cwd=excluded.cwd,
                    repo_root=excluded.repo_root, worktree_path=excluded.worktree_path,
                    branch=excluded.branch, created_at=excluded.created_at,
                    last_activity=excluded.last_activity,
                    message_count=excluded.message_count,
                    transcript_path=excluded.transcript_path,
                    resume_command=excluded.resume_command, status=excluded.status,
                    transcript_mtime=excluded.transcript_mtime,
                    transcript_size=excluded.transcript_size,
                    scanned_at=excluded.scanned_at
                """,
                rows,
            )
        return len(rows)

    def get_fingerprints(self, harness: str) -> dict[str, tuple[float, int]]:
        """transcript_path -> (mtime, size), para pular arquivos inalterados."""
        rows = self.conn.execute(
            """
            SELECT transcript_path, transcript_mtime, transcript_size
            FROM sessions
            WHERE harness = ? AND transcript_path IS NOT NULL
              AND transcript_mtime IS NOT NULL AND transcript_size IS NOT NULL
            """,
            (harness,),
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def list_sessions(
        self,
        status: str | None = None,
        harness: str | None = None,
        include_hidden: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if not include_hidden:
            where.append("s.hidden = 0")
        if status:
            where.append("s.status = ?")
            params.append(status)
        if harness:
            where.append("s.harness = ?")
            params.append(harness)
        sql = _SELECT_SESSIONS
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY (s.last_activity IS NULL), s.last_activity DESC"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def find_sessions(self, prefix: str) -> list[dict[str, Any]]:
        """Busca por id exato ou prefixo (case-insensitive p/ ASCII)."""
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = _SELECT_SESSIONS + " WHERE s.session_id LIKE ? ESCAPE '\\'"
        return [dict(r) for r in self.conn.execute(sql, (esc + "%",)).fetchall()]

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM sessions GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def distinct_harnesses(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT harness FROM sessions ORDER BY harness"
        ).fetchall()
        return [r[0] for r in rows]

    def set_note(self, harness: str, session_id: str, note: str, tags: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET note = ?, tags = ? WHERE harness = ? AND session_id = ?",
                (note, tags, harness, session_id),
            )

    def set_hidden(self, harness: str, session_id: str, hidden: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET hidden = ? WHERE harness = ? AND session_id = ?",
                (1 if hidden else 0, harness, session_id),
            )

    def recompute_status(self, forgotten_hours: float) -> None:
        """Status derivado (fase 3 refina com hooks/processos)."""
        cutoff = (datetime.now(UTC) - timedelta(hours=forgotten_hours)).isoformat()
        with self.conn:
            self.conn.execute(
                """
                UPDATE sessions
                SET status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM worktrees w
                        WHERE w.path = sessions.worktree_path
                          AND (w.staged + w.unstaged + w.untracked) > 0
                    )
                     AND (last_activity IS NULL OR last_activity < ?)
                    THEN 'forgotten'
                    ELSE 'stopped'
                END
                WHERE status IN ('unknown', 'stopped', 'forgotten')
                """,
                (cutoff,),
            )

    def known_repos(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT repo_root FROM worktrees WHERE repo_root IS NOT NULL
            UNION
            SELECT DISTINCT repo_root FROM sessions WHERE repo_root IS NOT NULL
            """
        ).fetchall()
        return [r[0] for r in rows]

    # ---------------------------------------------------------- worktrees

    def replace_worktrees(self, repo_root: str, worktrees: list[Worktree]) -> None:
        now = _iso(datetime.now(UTC))
        with self.conn:
            self.conn.execute("DELETE FROM worktrees WHERE repo_root = ?", (repo_root,))
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO worktrees (path, repo_root, branch, head, staged,
                    unstaged, untracked, ahead, behind, pr_number, pr_url, scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        w.path,
                        w.repo_root,
                        w.branch,
                        w.head,
                        w.staged,
                        w.unstaged,
                        w.untracked,
                        w.ahead,
                        w.behind,
                        w.pr_number,
                        w.pr_url,
                        now,
                    )
                    for w in worktrees
                ],
            )

    def list_worktrees(self, repo_root: str | None = None) -> list[dict[str, Any]]:
        if repo_root:
            rows = self.conn.execute(
                "SELECT * FROM worktrees WHERE repo_root = ? ORDER BY path", (repo_root,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM worktrees ORDER BY path").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- eventos (fase 3)

    def record_event(
        self,
        ts: str,
        harness: str,
        session_id: str,
        event: str,
        cwd: str | None = None,
        transcript_path: str | None = None,
        payload: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO events (ts, harness, session_id, event, cwd,
                                    transcript_path, payload)
                VALUES (?,?,?,?,?,?,?)
                """,
                (ts, harness, session_id, event, cwd, transcript_path, payload),
            )

    def last_events(self, harness: str, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT ts, event, cwd, transcript_path, payload FROM events
            WHERE harness = ? AND session_id = ?
            ORDER BY ts DESC LIMIT ?
            """,
            (harness, session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def counts(self) -> tuple[int, int]:
        """(total de sessões, total de worktrees)."""
        s = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        w = self.conn.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0]
        return int(s), int(w)
