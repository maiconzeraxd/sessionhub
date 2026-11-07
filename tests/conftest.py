"""Fixtures do SessionHub (tudo sintético, em tmp_path)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import git

from sessionhub.config import Config
from sessionhub.db import Database


@pytest.fixture(autouse=True)
def _never_open_real_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rede de segurança: teste nenhum abre terminal/editor de verdade.

    Bug real de 2026-11-07: Tree.select_node posta NodeSelected e o rebuild
    restaurava a "seleção" — a TUI abria `wt.exe -d <tmp> cmd /k claude
    --resume <id-sintético>` em pleno pytest. Mesco com o bug corrigido,
    nenhum teste pode spawnar processo real.
    """
    stub = SimpleNamespace(status="stub", command="", message="desabilitado em teste")
    monkeypatch.setattr("sessionhub.tui.open_session", lambda *a, **k: stub)
    monkeypatch.setattr("sessionhub.tui.open_in_editor", lambda *a, **k: stub)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "index.db")
    yield database
    database.close()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    git("add", "README.md", cwd=repo)
    git("commit", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def config(tmp_path: Path, db: Database) -> Config:
    home = tmp_path / "sh"
    return Config(
        home=home,
        db_path=db.path,
        log_dir=home / "logs",
        backup_dir=home / "backups",
    )
