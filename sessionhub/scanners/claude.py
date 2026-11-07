"""Scanner do Claude Code (`~/.claude/projects/**/*.jsonl`).

Cada linha do transcript é um JSON. Campos usados (presentes nas linhas
user/assistant): `cwd`, `gitBranch`, `timestamp`, `message.content` (string
ou lista de blocos `{type: text, text}`). `gitBranch` "HEAD" significa branch
desconhecida — ignoramos e deixamos o git resolver. O `sessionId` interno NÃO
é usado como id: o id é o nome do arquivo (é ele que o `claude --resume`
procura), porque o sessionId de linhas de subagente é o da sessão pai.

O título é a primeira mensagem real do usuário (pulando isMeta, comandos
locais `<command-*>` e subagentes), com fallback para o campo `summary`.

Performance: as linhas passam por um pre-filtro de substring antes do
`json.loads`; transcripts inalterados (mtime+size iguais ao índice) não são
relidos. Linhas corrompidas são puladas e registradas no log.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ..log import get_logger
from ..models import STATUS_STOPPED, DiscoverResult, HarnessDiagnosis, Session

log = get_logger(__name__)

TITLE_MAX = 80

# Prefixos de conteúdo que não são mensagens reais do usuário.
SKIP_TITLE_PREFIXES = (
    "<command-name>",
    "<command-message",
    "<command-args>",
    "<local-command",
    "<local-command-caveat>",
    "Caveat:",
    "<user-memory-input>",
    "<system-reminder>",
    "<task-notification>",
)

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
_PASTE_OPEN_RE = re.compile(r"^\s*<pasted_content[^>]*>\s*")


def strip_wrappers(text: str) -> str:
    """Remove invólucros de hook do texto: blocos <system-reminder> e o
    wrapper <pasted_content> (mantém o conteúdo interno do paste)."""
    out = _REMINDER_RE.sub(" ", text)
    out = _PASTE_OPEN_RE.sub("", out, count=1)
    if "</pasted_content>" in out:
        inner, _, tail = out.partition("</pasted_content>")
        out = inner if inner.strip() else tail
    return out

# Marcadores de linha que vale a pena parsear (formato compact do Claude Code).
INTERESTING_MARKERS = (
    '"type":"user"',
    '"type": "user"',
    '"type":"assistant"',
    '"type": "assistant"',
    '"type":"summary"',
    '"type": "summary"',
)


def content_text(content: object) -> str:
    """message.content → texto (string ou blocos {text})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def clean_title(text: str) -> str | None:
    """Normaliza e valida um candidato a título (None se não servir)."""
    one = " ".join(strip_wrappers(text).split())
    if not one:
        return None
    for prefix in SKIP_TITLE_PREFIXES:
        if one.startswith(prefix):
            return None
    return one[:TITLE_MAX]


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class ClaudeScanner:
    """Descobre sessões do Claude Code."""

    name = "claude"

    def __init__(self, user_home: Path | None = None, projects_dir: Path | None = None) -> None:
        home = Path(user_home) if user_home is not None else Path.home()
        self.projects_dir = (
            projects_dir if projects_dir is not None else home / ".claude" / "projects"
        )

    def diagnose(self) -> HarnessDiagnosis:
        """Estado da origem do harness (para o doctor)."""
        root = self.projects_dir
        files = sum(1 for _ in root.rglob("*.jsonl")) if root.is_dir() else 0
        note: str | None = None
        problem = False
        if not root.is_dir():
            note = "não instalado (ou sem ~/.claude/projects)"
        elif files == 0:
            note = "projects existe mas nenhum transcript .jsonl"
            problem = True
        return HarnessDiagnosis(
            name=self.name,
            found=root.is_dir(),
            path=str(root),
            files=files,
            note=note,
            problem=problem,
        )

    def discover(self, fingerprints: dict[str, tuple[float, int]] | None = None) -> DiscoverResult:
        result = DiscoverResult()
        if not self.projects_dir.is_dir():
            return result
        for fp in sorted(self.projects_dir.rglob("*.jsonl")):
            # subagentes são sidechains (o sessionId interno é o da sessão
            # pai), não são sessões e não devem poluir o índice. Dois formatos:
            # pasta `subagents/` (atual) e `agent-*.jsonl` solto na pasta do
            # projeto (versões antigas do Claude Code).
            if "subagents" in fp.relative_to(self.projects_dir).parts or fp.name.startswith(
                "agent-"
            ):
                continue
            key = str(fp)
            result.seen.add(key)
            try:
                stat = fp.stat()
            except OSError as exc:
                log.warning("stat falhou em %s: %s", key, exc)
                result.errors += 1
                continue
            if fingerprints is not None:
                prev = fingerprints.get(key)
                if prev is not None and prev == (stat.st_mtime, stat.st_size):
                    result.skipped += 1
                    continue
            try:
                session = self.parse_file(fp, stat)
            except Exception as exc:  # transcript ilegível: pula sem quebrar o scan
                log.warning("parse falhou em %s: %s", key, exc)
                result.errors += 1
                continue
            if session is not None:
                result.sessions.append(session)
        self._dedup(result)
        return result

    @staticmethod
    def _dedup(result: DiscoverResult) -> None:
        """Vários arquivos podem alegar o mesmo sessionId — fica o mais recente."""
        if not result.sessions:
            return
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        best: dict[str, Session] = {}
        for s in result.sessions:
            cur = best.get(s.session_id)
            if cur is None or (s.last_activity or epoch) > (cur.last_activity or epoch):
                best[s.session_id] = s
        result.sessions = list(best.values())

    def parse_file(self, fp: Path, stat: os.stat_result | None = None) -> Session | None:
        """Lê um transcript e devolve a Session (None se vazio/ilegível).

        O id da sessão é o **nome do arquivo** (stem): é ele que o
        `claude --resume` procura. O `sessionId` interno das linhas pode ser o
        da sessão pai (subagente engolindo a sessão principal) — nunca é usado
        como id. Se TODAS as mensagens são sidechain, o arquivo é um
        subagente em formato antigo e não é uma sessão (None).
        """
        st = os.stat(fp) if stat is None else stat
        session_id = fp.stem
        cwd: str | None = None
        branch: str | None = None
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        title: str | None = None
        summary_title: str | None = None
        message_count = 0
        sidechain_msgs = 0
        bad_lines = 0

        with fp.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not any(marker in raw for marker in INTERESTING_MARKERS):
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
                if not isinstance(rec, dict):
                    continue
                rtype = rec.get("type")
                ts = parse_timestamp(rec.get("timestamp"))
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                c = rec.get("cwd")
                if isinstance(c, str) and c:
                    cwd = c
                gb = rec.get("gitBranch")
                if isinstance(gb, str) and gb and gb != "HEAD":
                    branch = gb
                if rtype in ("user", "assistant"):
                    sidechain = bool(rec.get("isSidechain"))
                    is_meta = bool(rec.get("isMeta"))
                    if sidechain:
                        sidechain_msgs += 1
                    elif not is_meta:
                        message_count += 1
                    if rtype == "user" and title is None and not sidechain and not is_meta:
                        msg = rec.get("message")
                        if isinstance(msg, dict):
                            cand = clean_title(content_text(msg.get("content")))
                            if cand:
                                title = cand
                elif rtype == "summary":
                    s = rec.get("summary")
                    if isinstance(s, str) and s and summary_title is None:
                        summary_title = s[:TITLE_MAX]

        if bad_lines:
            log.info("%d linhas corrompidas em %s", bad_lines, fp)

        # arquivo inteiro é sidechain de subagente (formato antigo): não é sessão
        if sidechain_msgs and message_count == 0:
            return None
        if title is None:
            title = summary_title
        if last_ts is None:
            last_ts = datetime.fromtimestamp(float(getattr(st, "st_mtime", 0.0)), tz=UTC)
        if first_ts is None:
            first_ts = last_ts

        return Session(
            harness=self.name,
            session_id=session_id,
            title=title,
            cwd=cwd,
            branch=branch,
            created_at=first_ts,
            last_activity=last_ts,
            message_count=message_count,
            transcript_path=str(fp),
            resume_command=f"claude --resume {session_id}",
            status=STATUS_STOPPED,
            transcript_mtime=float(getattr(st, "st_mtime", 0.0)),
            transcript_size=int(getattr(st, "st_size", 0)),
        )
