"""Modelos de dados centrais do SessionHub."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Status de sessão (ícones correspondentes na TUI).
STATUS_RUNNING = "running"  # 🟢 processo vivo / hook UserPromptSubmit
STATUS_WAITING = "waiting"  # 🟡 hook Notification ou Stop (aguarda você)
STATUS_STOPPED = "stopped"  # ⚪ sem processo vivo
STATUS_FORGOTTEN = "forgotten"  # 🔴 worktree sujo + N horas sem atividade
STATUS_UNKNOWN = "unknown"
STATUS_MISSING = "missing"  # 🗄 transcript não existe mais na origem

STATUS_ICONS: dict[str, str] = {
    STATUS_RUNNING: "🟢",
    STATUS_WAITING: "🟡",
    STATUS_STOPPED: "⚪",
    STATUS_FORGOTTEN: "🔴",
    STATUS_UNKNOWN: "❔",
    STATUS_MISSING: "🗄",
}


@dataclass
class Session:
    """Uma sessão de conversa com um agente de código."""

    harness: str
    session_id: str
    title: str | None = None
    cwd: str | None = None
    repo_root: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    created_at: datetime | None = None
    last_activity: datetime | None = None
    message_count: int = 0
    transcript_path: str | None = None
    resume_command: str | None = None
    status: str = STATUS_STOPPED
    transcript_mtime: float | None = None
    transcript_size: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.harness, self.session_id)


@dataclass
class Worktree:
    """Um worktree git, com ou sem sessões associadas."""

    repo_root: str
    path: str
    branch: str | None = None
    head: str | None = None
    head_ts: datetime | None = None  # data do último commit (idade do worktree)
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    ahead: int | None = None
    behind: int | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    forgotten: bool = False  # trabalho não entregue + inativo há muito tempo

    @property
    def dirty(self) -> int:
        return self.staged + self.unstaged + self.untracked

    @property
    def needs_attention(self) -> bool:
        """Mudanças locais OU commits não enviados — algo a perder."""
        return self.dirty > 0 or (self.ahead or 0) > 0


@dataclass
class ScanStats:
    """Resultado de uma passada de scan."""

    sessions: int = 0  # novas/alteradas processadas
    sessions_skipped: int = 0  # inalteradas (índice quente)
    repos: int = 0
    worktrees: int = 0
    errors: int = 0
    duration: float = 0.0
    harnesses: list[str] = field(default_factory=list)


@dataclass
class DiscoverResult:
    """Saída de um scanner: apenas sessões novas/alteradas.

    `seen` carrega os paths de transcripts encontrados (mesmo inalterados),
    para o chamador saber o que ainda existe na origem.
    """

    sessions: list[Session] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    skipped: int = 0
    errors: int = 0


@dataclass
class HarnessDiagnosis:
    """Saída do diagnose() de um scanner — alimenta o `doctor`."""

    name: str
    found: bool  # a origem do harness existe nesta máquina
    path: str  # caminho observado
    files: int  # transcripts encontrados (0 quando não encontrado)
    note: str | None = None  # observação (dim) ou problema (vermelho)
    problem: bool = False  # True → o note entra como problema, não como nota
