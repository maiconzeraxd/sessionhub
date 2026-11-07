"""Watcher de arquivos: transcripts mudaram → scan quente na TUI.

Usa `watchdog` (dependência do projeto). Se não estiver instalado (instalação
parcial), é um no-op silencioso: o fallback da TUI continua sendo o refresh
periódico de 5s comparando a assinatura do índice.

O watcher NUNCA escreve nos diretórios dos harnesses — só observa.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger(__name__)

try:  # dependência: presente em instalação normal
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except ImportError:  # pragma: no cover - instalação sem deps
    _HAS_WATCHDOG = False


if _HAS_WATCHDOG:

    class _CallbackHandler(FileSystemEventHandler):
        """Delega eventos relevantes (.jsonl/.zst) a um callback.

        O callback roda na thread do observer — precisa ser thread-safe
        (a TUI embute o call_soon_threadsafe).
        """

        def __init__(self, on_change: Callable[[], None]) -> None:
            super().__init__()
            self._on_change = on_change

        def on_any_event(self, event: FileSystemEvent) -> None:
            # só transcripts importam; eventos de diretório não
            if event.is_directory:
                return
            name = str(getattr(event, "src_path", "") or "")
            if name.endswith(".jsonl") or name.endswith(".zst"):
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001 - callback nunca derruba o observer
                    log.exception("callback do watcher falhou")

else:  # pragma: no cover

    class _CallbackHandler:  # type: ignore[no-redef]
        """Stub: nunca usado sem watchdog (start devolve False antes)."""

        def __init__(self, on_change: Callable[[], None]) -> None:
            self._on_change = on_change


class TranscriptWatcher:
    """Observa pastas de transcripts (ex.: ~/.claude/projects)."""

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._observer: Any | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._observer is not None

    def start(self, paths: list[Path]) -> bool:
        """Observa as pastas que existem. False = nada a observar (ou sem
        watchdog instalado)."""
        if not _HAS_WATCHDOG:
            log.debug("watchdog não instalado — watcher desativado (fallback: 5s)")
            return False
        existing = [p for p in paths if p.is_dir()]
        if not existing:
            return False
        with self._lock:
            if self._observer is not None:
                return True
            observer: Any = Observer()
            handler = _CallbackHandler(self._on_change)
            for path in existing:
                observer.schedule(handler, str(path), recursive=True)
            observer.start()
            self._observer = observer
            log.info("watcher ativo em %d pasta(s)", len(existing))
        return True

    def stop(self) -> None:
        with self._lock:
            if self._observer is not None:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=2.0)
                except Exception:  # noqa: BLE001 - desligar nunca explode
                    log.exception("erro ao parar o watcher")
                self._observer = None
