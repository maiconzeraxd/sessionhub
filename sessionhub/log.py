"""Logging do SessionHub (arquivo em ~/.sessionhub/logs/sessionhub.log)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "sessionhub"


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Logger do app; sem setup, só NullHandler (silencioso em biblioteca)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    """Instala handler rotativo em arquivo (e opcional no stderr)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    file_handler = RotatingFileHandler(
        log_dir / "sessionhub.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)
    if verbose:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(stream_handler)
    return logger
