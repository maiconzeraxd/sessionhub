"""Registry de scanners de harness.

Um scanner descobre transcripts de um agente de código e devolve
`DiscoverResult` (só sessões novas/alteradas). Harnesses novos entram:

1. builtin — módulo nesta package registrado em `builtin_scanners`;
2. plugin — entry point do grupo `sessionhub.scanners` (instalável à parte).

Nenhum dos dois exige mexer no núcleo (scan/cli não conhecem harnesses).
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..log import get_logger
from ..models import DiscoverResult, HarnessDiagnosis

log = get_logger(__name__)


@runtime_checkable
class Scanner(Protocol):
    """Contrato de um scanner de harness.

    `diagnose` é opcional para plugins (o doctor usa getattr) — builtins
    sempre implementam.
    """

    name: str

    def discover(
        self, fingerprints: dict[str, tuple[float, int]] | None = None
    ) -> DiscoverResult: ...

    def diagnose(self) -> HarnessDiagnosis: ...


def builtin_scanners(user_home: Path | None = None) -> list[Scanner]:
    """Scanners que acompanham o SessionHub."""
    from .claude import ClaudeScanner

    scanners: list[Scanner] = []
    scanners.append(ClaudeScanner(user_home))
    return scanners


def entrypoint_scanners() -> list[Scanner]:
    """Scanners de terceiros via entry point `sessionhub.scanners`."""
    out: list[Scanner] = []
    try:
        eps = entry_points(group="sessionhub.scanners")
    except Exception:  # pragma: no cover - ambiente exótico
        return out
    for ep in eps:
        try:
            obj = ep.load()
            scanner = obj() if callable(obj) else obj
            if hasattr(scanner, "name") and hasattr(scanner, "discover"):
                out.append(scanner)
            else:
                log.warning("entry-point %s não implementa Scanner", ep.name)
        except Exception as exc:
            log.warning("entry-point %s falhou ao carregar: %s", ep.name, exc)
    return out


def available_scanners(user_home: Path | None = None) -> list[Scanner]:
    """Todos os scanners ativos (builtin + plugins)."""
    return builtin_scanners(user_home) + entrypoint_scanners()
