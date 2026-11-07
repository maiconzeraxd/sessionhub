"""Testes do watcher (watchdog instalado — eventos reais em tmp)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import DirModifiedEvent, FileModifiedEvent

from sessionhub.watcher import TranscriptWatcher, _CallbackHandler


def test_callback_handler_filtra_jsonl() -> None:
    """Só eventos de arquivo .jsonl/.zst disparam o callback."""
    hits: list[str] = []
    handler = _CallbackHandler(lambda: hits.append("x"))
    handler.on_any_event(FileModifiedEvent("/tmp/proj/abc.jsonl"))
    handler.on_any_event(FileModifiedEvent("/tmp/proj/rollout-1.jsonl.zst"))
    handler.on_any_event(FileModifiedEvent("/tmp/proj/leia-me.txt"))
    handler.on_any_event(DirModifiedEvent("/tmp/proj"))
    assert hits == ["x", "x"]


def test_callback_handler_callback_que_nao_explode() -> None:
    """Callback que lança não derruba o handler (observer segue vivo)."""
    def boom() -> None:
        raise RuntimeError("boom")

    handler = _CallbackHandler(boom)
    handler.on_any_event(FileModifiedEvent("/tmp/x.jsonl"))  # não deve propagar


def test_watcher_sem_pastas_nao_inicia(tmp_path: Path) -> None:
    w = TranscriptWatcher(lambda: None)
    assert w.start([tmp_path / "nao-existe"]) is False
    assert w.active is False


def test_watcher_evento_real_dispara_callback(tmp_path: Path) -> None:
    """Ponta a ponta: escrever .jsonl na pasta observada chama o callback
    (deadline interno de 10s — não depende de plugin de timeout)."""
    projects = tmp_path / "projects"
    projects.mkdir()
    fired = threading.Event()

    w = TranscriptWatcher(fired.set)
    assert w.start([projects]) is True
    try:
        (projects / "sessao.jsonl").write_text("{}\n", encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while not fired.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        w.stop()
    assert fired.is_set(), "callback deveria ter disparado ao escrever .jsonl"
    assert w.active is False


def test_watcher_stop_sem_start_e_idempotente() -> None:
    w = TranscriptWatcher(lambda: None)
    w.stop()  # não explode
    w.stop()
