"""Backup de sessões: transcript → Markdown, um .md por sessão.

Cria `<destino>/<AAAA-MM-DD_HH-MM>/` e, dentro dela, um .md por sessão
(nome do arquivo = título sanitizado + prefixo do id). A conversa é
transcrita em streaming — transcripts de GB não estouram a memória — e
por completo (diferente do preview, que só lê o final).

Contrato de escrita: somente no destino do backup (config.toml →
[backup] dir; padrão dentro de ~/.sessionhub). Os transcripts dos
harnesses são apenas LIDOS — o SessionHub nunca escreve neles.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .fmt import age, human_size
from .log import get_logger
from .models import STATUS_ICONS
from .scanners.claude import INTERESTING_MARKERS, content_text, parse_timestamp

log = get_logger(__name__)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class BackupResult:
    """Um backup: pasta criada, .mds gravados (1:1 com `done`) e falhas."""

    folder: Path
    done: list[dict[str, Any]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def backup_sessions(
    rows: list[dict[str, Any]], dest_root: Path | str, now: datetime | None = None
) -> BackupResult:
    """Grava cada sessão como .md dentro de <dest_root>/<data+hora>/."""
    when = now or datetime.now(UTC)
    folder = _dest_folder(Path(dest_root), when)
    folder.mkdir(parents=True, exist_ok=True)
    result = BackupResult(folder=folder)
    seen: set[str] = set()
    for row in rows:
        tp = str(row.get("transcript_path") or "")
        if not tp or not os.path.isfile(tp):
            # sem transcript não há conversa para backupear — erro explícito
            # (silenciar aqui daria falsa sensação de backup feito)
            result.errors.append(
                f"{row.get('title') or row.get('session_id')}: transcript não existe mais"
            )
            continue
        try:
            fp = _write_session_md(folder, row, seen, when)
        except OSError as exc:
            log.warning("backup de %s falhou: %s", row.get("session_id"), exc)
            result.errors.append(f"{row.get('title') or row.get('session_id')}: {exc}")
            continue
        result.done.append(row)
        result.files.append(fp)
    return result


def _dest_folder(root: Path, now: datetime) -> Path:
    """Pasta do backup com a data (AAAA-MM-DD_HH-MM); colisão ganha -N."""
    base = now.strftime("%Y-%m-%d_%H-%M")
    folder = root / base
    n = 2
    while folder.exists():
        folder = root / f"{base}-{n}"
        n += 1
    return folder


def _filename(row: dict[str, Any], seen: set[str]) -> str:
    """Título sanitizado + prefixo do id — títulos iguais não colidem."""
    sid = str(row.get("session_id") or "sessao")
    base = _slug(str(row.get("title") or sid))
    name = f"{base}__{sid[:8]}.md"
    n = 2
    while name in seen:
        name = f"{base}__{sid[:8]}-{n}.md"
        n += 1
    seen.add(name)
    return name


def _slug(title: str) -> str:
    """Título → nome de arquivo seguro (remove os proibidos do Windows)."""
    slug = _UNSAFE.sub("-", title)
    slug = re.sub(r"\s+", " ", slug).strip(" .")
    return slug[:60].strip() or "sessao"


def _fmt_dt(iso: str | None) -> str:
    dt = parse_timestamp(iso) if iso else None
    return dt.strftime("%d/%m/%Y %H:%M") if dt else "—"


def _header(row: dict[str, Any], now: datetime) -> str:
    title = (row.get("title") or "(sem título)").replace("|", "\\|")
    size: int | None = None
    tp = row.get("transcript_path")
    if tp:
        try:
            size = os.path.getsize(str(tp))
        except OSError:
            size = None
    icon = STATUS_ICONS.get(row.get("status") or "", "")
    out = [
        f"# {title}",
        "",
        f"> backup SessionHub · gerado em {now.strftime('%d/%m/%Y %H:%M')} UTC",
        "",
        f"- **sessão:** `{row.get('session_id')}` · **harness:** {row.get('harness')}"
        f" · **status:** {icon} {row.get('status')}",
        f"- **criada em:** {_fmt_dt(row.get('created_at'))}"
        f" · **última atividade:** {_fmt_dt(row.get('last_activity'))}"
        f" ({age(row.get('last_activity'))} atrás)",
        f"- **mensagens:** {row.get('message_count', 0)} · **tamanho:** {human_size(size)}",
        f"- **branch:** `{row.get('branch') or '—'}`"
        f" · **worktree:** `{row.get('worktree_path') or row.get('cwd') or '—'}`",
    ]
    if row.get("note"):
        out.append(f"- 📌 **nota:** {row['note']}")
    if row.get("tags"):
        out.append(f"- **tags:** {row['tags']}")
    if row.get("resume_command"):
        out.append(f"- **retomada:** `{row['resume_command']}`")
    return "\n".join(out) + "\n"


def _write_session_md(
    folder: Path, row: dict[str, Any], seen: set[str], now: datetime
) -> Path:
    fp = folder / _filename(row, seen)
    with open(fp, "w", encoding="utf-8", newline="\n") as out:
        out.write(_header(row, now))
        out.write("\n## 💬 Conversa completa\n")
        n = _stream_conversation(out, row.get("transcript_path"), str(row.get("harness")))
        if n == 0:
            out.write("\n_transcript vazio, ilegível ou de harness ainda sem suporte._\n")
    return fp


def _stream_conversation(
    out: TextIO, transcript_path: str | None, harness: str
) -> int:
    """Transcreve a conversa inteira (streaming: uma mensagem por vez em RAM)."""
    if not transcript_path or harness != "claude" or not os.path.isfile(transcript_path):
        return 0
    n = 0
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
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
            body = content_text(msg.get("content")).strip() if isinstance(msg, dict) else ""
            if not body:
                continue
            who = "👤 usuário" if rtype == "user" else "🤖 assistente"
            ts = rec.get("timestamp")
            dt = parse_timestamp(ts) if isinstance(ts, str) else None
            when = f" · {dt.strftime('%d/%m/%Y %H:%M')}" if dt else ""
            out.write(f"\n**{who}**{when}\n\n")
            for chunk in body.splitlines():
                out.write(f"> {chunk}\n" if chunk else ">\n")
            n += 1
    return n
