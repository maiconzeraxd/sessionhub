"""Testes da TUI (pilot do Textual, tudo em tmp)."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Tree

from sessionhub.config import Config
from sessionhub.db import Database
from sessionhub.models import Session
from sessionhub.tui import NodeData, SessionHubApp, SessionTree

SID = "11111111-2222-3333-4444-555555555555"


def _fill_db(db: Database, tmp_path: Path) -> None:
    from datetime import datetime

    now = datetime.now(UTC)
    db.upsert_sessions(
        [
            Session(
                harness="claude",
                session_id=SID,
                title="conserta scanner",
                cwd=str(tmp_path),
                repo_root=str(tmp_path),
                worktree_path=str(tmp_path),
                branch="main",
                last_activity=now,
                message_count=4,
                transcript_path=str(tmp_path / "t.jsonl"),
                resume_command=f"claude --resume {SID}",
                status="waiting",
            ),
            Session(
                harness="claude",
                session_id="99999999-2222-3333-4444-555555555555",
                title="outra tarefa",
                cwd=str(tmp_path),
                repo_root=str(tmp_path),
                worktree_path=str(tmp_path),
                branch="main",
                last_activity=now,
                message_count=2,
                resume_command="claude --resume 99999999",
                status="stopped",
            ),
        ]
    )


def _app(config: Config) -> SessionHubApp:
    return SessionHubApp(config=config)


async def test_tui_monta_arvore_com_sessoes(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        assert tree.root.children, "deveria ter pelo menos um repo"
        # encontra nós de sessão
        texts = [
            str(child.data.row["session_id"])
            for node in tree.root.children
            for wt in node.children
            for child in wt.children
            if child.data and getattr(child.data, "kind", "") == "session"
        ]
        assert SID in texts
        summary_text = getattr(app, "_summary_text", "")
        assert "esperando você" in summary_text


async def test_tui_filtro_esperando(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_filter_waiting()
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        session_titles = [
            child.data.row["title"]
            for node in tree.root.children
            for wt in node.children
            for child in wt.children
            if child.data and getattr(child.data, "kind", "") == "session"
        ]
        assert session_titles == ["conserta scanner"]


async def test_tui_busca_fuzzy_filtra(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "cnsle"  # subsequência de "conserta scanner"
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        titles = [
            child.data.row["title"]
            for node in tree.root.children
            for wt in node.children
            for child in wt.children
            if child.data and getattr(child.data, "kind", "") == "session"
        ]
        assert titles == ["conserta scanner"]


async def test_tui_tecla_x_arquiva(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = db.find_sessions(SID)[0]
        app._current_row = row
        app.action_hide()
        await pilot.pause()
        assert db.find_sessions(SID)[0]["hidden"] == 1
        # agora a listagem padrão não mostra
        assert len(db.list_sessions()) == 1


async def test_tui_nota_salva_no_banco(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = db.find_sessions(SID)[0]
        app._on_note_result(("minha nota", "urgente"))
        await pilot.pause()
        row = db.find_sessions(SID)[0]
        assert row["note"] == "minha nota"
        assert row["tags"] == "urgente"


async def test_tui_tecla_4_filtra_todas(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        # sem crash; árvore ainda renderiza
        assert app.query_one("#tree", Tree)


@pytest.mark.parametrize("key", ["1", "2", "3", "4", "s", "r", "c", "o", "n", "x", "b", "H"])
async def test_tui_atalhos_nao_quebram(
    db: Database, tmp_path: Path, config: Config, key: str
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        # foca a árvore p/ as teclas irem pro widget certo
        app.query_one("#tree", Tree).focus()
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert app.is_running


class _StubClick:
    """Click sintético: só o que SessionTree._on_click lê (style.meta, chain)."""

    class _Style:
        def __init__(self, meta: dict) -> None:
            self.meta = meta

    def __init__(self, line: int, chain: int) -> None:
        self.style = self._Style({"line": line})
        self.chain = chain


async def test_tui_clique_unico_navega_duplo_abre(
    db: Database, tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clique único só move o cursor (preview); duplo-clique abre a sessão."""
    _fill_db(db, tmp_path)
    opened: list[str] = []
    ok = SimpleNamespace(status="opened", command="cmd", message="ok")
    monkeypatch.setattr(
        "sessionhub.tui.open_session",
        lambda row, cfg, **kw: opened.append(str(row["session_id"])) or ok,
    )
    app = _app(config)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", SessionTree)
        target = None
        for line in range(tree.last_line + 1):
            node = tree.get_node_at_line(line)
            data = getattr(node, "data", None)
            if isinstance(data, NodeData) and data.kind == "session":
                target = line
                break
        assert target is not None, "deveria achar um nó de sessão"

        await tree._on_click(_StubClick(target, 1))  # clique único
        await pilot.pause()
        assert app._current_row is not None  # preview atualizou
        assert opened == []  # mas nada abriu

        await tree._on_click(_StubClick(target, 2))  # duplo-clique
        await pilot.pause()
        assert opened == [SID]
