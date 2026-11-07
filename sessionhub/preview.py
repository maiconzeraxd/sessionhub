"""Pré-visualização de sessões: últimas mensagens + resumo + git.

Lê apenas o final do transcript (últimos bytes), então funciona bem em
arquivos enormes. A linha parcial do início do trecho é descartada.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .fmt import age, human_size
from .log import get_logger
from .models import STATUS_ICONS
from .scanners.claude import (
    INTERESTING_MARKERS,
    SKIP_TITLE_PREFIXES,
    content_text,
    parse_timestamp,
    strip_wrappers,
)

log = get_logger(__name__)

MAX_TAIL_BYTES = 2_000_000
MAX_HEAD_BYTES = 256_000
MAX_MESSAGES = 25
MAX_MESSAGE_CHARS = 1_200


@dataclass
class PreviewMessage:
    role: str  # "user" | "assistant"
    text: str
    ts: str | None


def tail_messages(
    transcript_path: str | None,
    harness: str,
    max_messages: int = MAX_MESSAGES,
    max_bytes: int = MAX_TAIL_BYTES,
) -> list[PreviewMessage]:
    """Últimas mensagens da conversa (claude; outros harnesses na fase 5)."""
    if not transcript_path or harness != "claude" or not os.path.isfile(transcript_path):
        return []
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
    except OSError as exc:
        log.warning("ler tail de %s falhou: %s", transcript_path, exc)
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # descarta linha parcial

    messages: list[PreviewMessage] = []
    for raw in lines:
        if not any(marker in raw for marker in INTERESTING_MARKERS):
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        if bool(rec.get("isSidechain")) or bool(rec.get("isMeta")):
            continue
        msg = rec.get("message")
        body = content_text(msg.get("content")) if isinstance(msg, dict) else ""
        body = body.strip()
        if not body:
            continue
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + " …"
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            dt = parse_timestamp(ts)
            ts = dt.strftime("%d/%m %H:%M") if dt else None
        else:
            ts = None
        messages.append(PreviewMessage(role=str(rtype), text=body, ts=ts))
    if max_messages <= 0:
        return []
    return messages[-max_messages:]


def first_user_message(
    transcript_path: str | None,
    harness: str,
    max_bytes: int = MAX_HEAD_BYTES,
) -> PreviewMessage | None:
    """A primeira mensagem real do usuário — o pedido inicial da sessão.

    Lê só o começo do transcript. Pula meta, sidechain, comandos locais e
    notificações de hook; desembrulha <pasted_content>/<system-reminder>.
    """
    if not transcript_path or harness != "claude" or not os.path.isfile(transcript_path):
        return None
    try:
        with open(transcript_path, "rb") as fh:
            data = fh.read(max_bytes)
    except OSError as exc:
        log.warning("ler head de %s falhou: %s", transcript_path, exc)
        return None
    text = data.decode("utf-8", errors="replace")
    for raw in text.splitlines():
        if '"type":"user"' not in raw and '"type": "user"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user":
            continue
        if bool(rec.get("isSidechain")) or bool(rec.get("isMeta")):
            continue
        msg = rec.get("message")
        body = strip_wrappers(content_text(msg.get("content")) if isinstance(msg, dict) else "")
        body = body.strip()
        if not body:
            continue
        if any(body.startswith(prefix) for prefix in SKIP_TITLE_PREFIXES):
            continue
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + " …"
        ts = rec.get("timestamp")
        ts_str: str | None = None
        if isinstance(ts, str):
            dt = parse_timestamp(ts)
            ts_str = dt.strftime("%d/%m %H:%M") if dt else None
        return PreviewMessage(role="user", text=body, ts=ts_str)
    return None


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = parse_timestamp(iso)
    if dt is None:
        return "—"
    local = dt.astimezone()
    return local.strftime("%d/%m/%Y %H:%M")


def preview_markdown(row: dict[str, Any], worktree: dict[str, Any] | None = None) -> str:
    """Painel de pré-visualização completo (markdown p/ o widget da TUI).

    Ordem pensada para "o que rolou por último": cabeçalho compacto →
    últimas mensagens → pedido inicial no fim (rola pra baixo para ver
    como a sessão começou).
    """
    icon = STATUS_ICONS.get(row.get("status") or "", "❔")
    title = (row.get("title") or "*_(sem título)_*").replace("|", "\\|")
    out: list[str] = [f"# {icon} {title}", ""]

    sid_short = str(row.get("session_id"))[:13]
    out.append(f"- **harness:** {row.get('harness')} · **id:** `{sid_short}…`")
    if row.get("branch"):
        out.append(f"- **branch:** `{row['branch']}`")
    wt = row.get("worktree_path") or row.get("cwd") or "—"
    exists = os.path.isdir(str(wt))
    mark = "" if exists else " ⚠ *(caminho não existe mais)*"
    out.append(f"- **worktree:** `{wt}`{mark}")
    out.append(
        f"- **atividade:** {age(row.get('last_activity'))} atrás"
        f" · criada em {_fmt_dt(row.get('created_at'))}"
    )
    size: int | None = None
    tp = row.get("transcript_path")
    if tp:
        try:
            size = os.path.getsize(str(tp))
        except OSError:
            size = None
    out.append(f"- **tamanho:** {human_size(size)} · {row.get('message_count', 0)} msgs")
    if row.get("note"):
        out.append(f"- 📌 **nota:** {row['note']}")
    if row.get("tags"):
        out.append(f"- **tags:** {row['tags']}")

    if worktree:
        dirty = (
            (worktree.get("staged") or 0)
            + (worktree.get("unstaged") or 0)
            + (worktree.get("untracked") or 0)
        )
        git_bits = [f"✎ {dirty} alterados"] if dirty else ["limpo"]
        if worktree.get("ahead"):
            git_bits.append(f"↑{worktree['ahead']}")
        if worktree.get("behind"):
            git_bits.append(f"↓{worktree['behind']}")
        if worktree.get("pr_number"):
            git_bits.append(f"PR #{worktree['pr_number']}")
        out.append(f"- **git:** {' · '.join(git_bits)}")

    if row.get("resume_command"):
        out.append("")
        out.append("```")
        out.append(str(row["resume_command"]))
        out.append("```")

    # o que interessa primeiro: as últimas mensagens da conversa
    out.append("")
    messages = tail_messages(row.get("transcript_path"), str(row.get("harness")))
    out.append("## 💬 Últimas mensagens")
    if not messages:
        out.append("_transcript vazio, ilegível ou de harness ainda sem suporte._")
    for m in messages:
        who = "👤 você" if m.role == "user" else "🤖 agente"
        when = f" _({m.ts})_" if m.ts else ""
        out.append("")
        out.append(f"**{who}**{when}")
        for chunk in m.text.splitlines() or [""]:
            out.append(f"> {chunk}")

    # como a sessão começou — no fim, fora da primeira dobra
    first = first_user_message(row.get("transcript_path"), str(row.get("harness")))
    if first and not (messages and messages[0].text == first.text):
        when = f" _({first.ts})_" if first.ts else ""
        out.append("")
        out.append("---")
        out.append("")
        out.append("## 🌱 Pedido inicial")
        out.append("")
        out.append(f"**👤 você**{when}")
        for chunk in first.text.splitlines():
            out.append(f"> {chunk}")
    return "\n".join(out)
