"""Exclusão de sessão: apaga do disco SÓ os arquivos da própria sessão.

A exceção única ao contrato read-only dos harnesses — autorizada
explicitamente pelo usuário ("excluir mesmo, não só da UI… não poderia
excluir nada além disso"). Por isso o escopo é mínimo e validado à prova
de erro:

- o transcript   <projects>/<sid>.jsonl
- a pasta irmã   <projects>/<sid>/   (custom-title.json do /rename etc.)

Nada além: a pasta do projeto, outros transcripts, `subagents/` e o git
do worktree nunca são tocados. Todo caminho é reconstruído a partir do
id (não confiamos no caminho gravado no índice) e o nome de tudo que é
removido deriva do id — id com separador ou ".." é recusado na hora.
A TUI pede confirmação mostrando os caminhos e, por padrão, grava um
backup .md antes (backup pedido que falhou aborta a exclusão).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .backup import backup_sessions
from .log import get_logger

log = get_logger(__name__)


@dataclass
class DeleteResult:
    """Uma exclusão: caminhos removidos, falhas e onde ficou o backup."""

    sid: str
    harness: str
    removed: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backup_folder: Path | None = None


def _safe_sid(sid: str) -> bool:
    """Id usável como nome de arquivo (sem traversal, sem separadores)."""
    return bool(sid) and sid not in (".", "..") and "/" not in sid and "\\" not in sid


def session_paths(row: dict[str, Any], user_home: Path | None = None) -> list[Path]:
    """Caminhos da PRÓPRIA sessão no disco (transcript + pasta do id).

    Cada caminho é reconstruído a partir do id dentro da pasta do projeto
    (derivada do transcript_path; sem transcript, varre os projetos do
    home atrás da pasta do id). Validados um a um — a pasta do projeto,
    outros transcripts e `subagents/` nunca entram na lista.
    """
    sid = str(row.get("session_id") or "")
    if not _safe_sid(sid) or row.get("harness") != "claude":
        return []
    parents: list[Path] = []
    tp = str(row.get("transcript_path") or "")
    if tp:
        parent = Path(tp).parent
        if parent.name:  # descarta raiz degenerada (path relativo sem pasta)
            parents.append(parent)
    else:
        # transcript sumiu (status missing): a pasta do id pode ter ficado —
        # varre os projetos do home (uuid: no máximo uma pasta bate)
        home = user_home or Path.home()
        projects = home / ".claude" / "projects"
        if projects.is_dir():
            parents = [p for p in projects.iterdir() if p.is_dir()]
    paths: list[Path] = []
    for parent in parents:
        fp = parent / f"{sid}.jsonl"
        if fp.is_file():
            paths.append(fp)
        folder = parent / sid
        if folder.is_dir() and folder != parent:
            paths.append(folder)
    return paths


def delete_session_files(
    row: dict[str, Any],
    user_home: Path | None = None,
    backup_dest: Path | None = None,
    backup_first: bool = True,
    now: datetime | None = None,
) -> DeleteResult:
    """Apaga os arquivos da sessão (e opcionalmente grava backup .md antes).

    Regras de segurança:
    - backup pedido e falhou → NADA é apagado (exclusão aborta);
    - transcript já sumido → segue só com a pasta do id;
    - harness sem layout conhecido → erro, nada acontece.
    """
    sid = str(row.get("session_id") or "")
    harness = str(row.get("harness") or "")
    result = DeleteResult(sid=sid, harness=harness)
    if harness != "claude":
        result.errors.append(f"harness {harness!r} sem exclusão suportada")
        return result
    paths = session_paths(row, user_home)
    if backup_first and backup_dest is not None:
        tp = str(row.get("transcript_path") or "")
        if os.path.isfile(tp):
            try:
                bk = backup_sessions([row], backup_dest, now=now)
            except OSError as exc:
                result.errors.append(f"backup falhou ({exc}) — nada foi apagado")
                return result
            if bk.files:
                result.backup_folder = bk.folder
            else:
                result.errors.append("backup não gravou nada — nada foi apagado")
                return result
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            result.errors.append(f"{path.name}: {exc}")
            continue
        result.removed.append(path)
        log.info("exclusão: %s", path)
    return result
