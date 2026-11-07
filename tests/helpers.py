"""Builders de fixtures sintéticas (nunca dados reais do usuário)."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any


def make_user_line(
    sid: str,
    cwd: str,
    text: str,
    ts: str = "2026-11-01T10:00:00.000Z",
    meta: bool = False,
    sidechain: bool = False,
    branch: str = "main",
) -> dict[str, Any]:
    return {
        "type": "user",
        "isSidechain": sidechain,
        "isMeta": meta,
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "cwd": cwd,
        "sessionId": sid,
        "gitBranch": branch,
        "uuid": str(uuid.uuid4()),
        "userType": "external",
        "version": "2.1.319",
    }


def make_assistant_line(sid: str, cwd: str, text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": ts,
        "cwd": cwd,
        "sessionId": sid,
        "uuid": str(uuid.uuid4()),
    }


def make_meta_line(sid: str) -> dict[str, Any]:
    return {"type": "mode", "mode": "normal", "sessionId": sid}


def make_summary_line(text: str) -> dict[str, Any]:
    return {"type": "summary", "summary": text, "leafUuid": str(uuid.uuid4())}


def write_transcript(path: Path, records: list[dict[str, Any] | str]) -> Path:
    """Grava linhas JSON; strings são escritas cruas (p/ simular corrupção)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            if isinstance(rec, str):
                fh.write(rec + "\n")
            else:
                fh.write(json.dumps(rec) + "\n")
    return path


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
