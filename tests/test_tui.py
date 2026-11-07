"""Testes da TUI (pilot do Textual, tudo em tmp)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import make_assistant_line, make_user_line, write_transcript
from textual.widgets import Input, Tree

from sessionhub.backup import BackupResult
from sessionhub.config import Config
from sessionhub.db import Database
from sessionhub.models import Session
from sessionhub.tui import (
    BackupScreen,
    DeleteScreen,
    NodeData,
    NoteScreen,
    SessionHubApp,
    SessionTree,
)

SID = "11111111-2222-3333-4444-555555555555"


async def _wait_until(
    pilot: Any, cond: Callable[[], object], timeout: float = 5.0
) -> None:
    """Espera determinística: pola a condição processando a fila de mensagens.

    Sleep fixo esperando debounce/to_thread vira flake quando a suíte inteira
    carrega a máquina (a margem some). Poll termina cedo quando verde e só
    estoura se a condição REALMENTE não chegou — o assert do chamador reporta.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await pilot.pause()
        await asyncio.sleep(0.01)


def _session_titles(app: SessionHubApp) -> list[str]:
    tree = app.query_one("#tree", Tree)
    return [
        str(child.data.row["title"])
        for node in tree.root.children
        for wt in node.children
        for child in wt.children
        if child.data and getattr(child.data, "kind", "") == "session"
    ]


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


def _app(config: Config, tmp_path: Path) -> SessionHubApp:
    """App isolado: user_home em tmp — watcher/scan nunca tocam o home real
    (durante os testes, os transcripts REAIS estão mudando; um scan quente
    no meio do teste reconstruía a árvore e quebrava o timing)."""
    return SessionHubApp(config=config, user_home=tmp_path / "home")


def test_fold_e_search_matches() -> None:
    """Dobra de acentos + subsequência (curto) vs substring (caminho)."""
    from sessionhub.tui import _fold, _search_matches

    assert _fold("Aplicação ÇÃ") == "aplicacao ca"
    assert _search_matches("cnsr", "conserta scanner", "")  # subsequência no curto
    assert _search_matches("aplicacao", "Analisa a Aplicação", "g:/x")  # sem acento
    assert _search_matches("north", "", "g:/dev/north/wt-a")  # substring no caminho
    # subsequência solta NÃO vale para caminho (senão casa com tudo)
    assert not _search_matches("nrt", "", "g:/dev/north")
    assert not _search_matches("zzz", "conserta scanner", "g:/dev/north")


async def test_tui_monta_arvore_com_sessoes(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
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
    app = _app(config, tmp_path)
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


async def test_tui_busca_fuzzy_filtra(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "cnsr"  # subsequência de "conserta"
        await _wait_until(pilot, lambda: _session_titles(app) == ["conserta scanner"])
        assert _session_titles(app) == ["conserta scanner"]


async def test_tui_busca_ignora_acentos(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """Digitar sem acento acha título com acento (pt-BR: 'aplicacao' → 'Aplicação')."""
    _fill_db(db, tmp_path)
    db.update_titles("claude", {str(tmp_path / "t.jsonl"): "Analisa a aplicação"})
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "aplicacao"
        await _wait_until(pilot, lambda: _session_titles(app) == ["Analisa a aplicação"])
        assert _session_titles(app) == ["Analisa a aplicação"]


async def test_tui_busca_esconde_worktree_sem_correspondencia(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """Durante a busca, worktree sem sessão/branch/caminho correspondente sai
    da árvore — senão a busca nunca estreia nada e parece morta."""
    _fill_db(db, tmp_path)
    from datetime import datetime

    from sessionhub.models import Worktree

    now = datetime.now(UTC)
    outro = str(tmp_path / "wt-outro")
    db.replace_worktrees(
        str(tmp_path),
        [
            Worktree(repo_root=str(tmp_path), path=str(tmp_path), branch="main", head_ts=now),
            Worktree(
                repo_root=str(tmp_path), path=outro, branch="feat/outra", head_ts=now
            ),
        ],
    )
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)

        def wt_count() -> int:
            return sum(len(node.children) for node in tree.root.children)

        assert wt_count() == 2  # sem busca: main + feat/outra

        search = app.query_one("#search", Input)
        search.value = "conserta"  # bate só com a sessão da worktree main
        await _wait_until(pilot, lambda: wt_count() == 1)
        assert wt_count() == 1  # feat/outra sumiu

        search.value = ""  # limpa → tudo volta
        await _wait_until(pilot, lambda: wt_count() == 2)
        assert wt_count() == 2


async def test_tui_busca_sem_resultado_mostra_aviso(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        search = app.query_one("#search", Input)
        search.value = "zzzz"

        def _aviso() -> bool:
            return any(
                "nenhum resultado" in str(node.label)
                for node in app._walk_nodes(tree.root)
            )

        await _wait_until(pilot, _aviso)
        labels = [str(node.label) for node in app._walk_nodes(tree.root)]
        assert any("nenhum resultado" in lbl for lbl in labels), labels


async def test_tui_busca_expande_worktree_colapsada(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """Resultado dentro de worktree colapsada precisa aparecer (a busca
    não restaura o colapso)."""
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", SessionTree)
        tree.root.children[0].children[0].collapse()
        await pilot.pause()

        search = app.query_one("#search", Input)
        search.value = "conserta"
        await _wait_until(
            pilot, lambda: tree.root.children[0].children[0].is_expanded
        )
        wt_node = tree.root.children[0].children[0]
        assert wt_node.is_expanded  # a busca expandiu
        session_nodes = [c for c in wt_node.children if c.data and c.data.kind == "session"]
        assert session_nodes  # e o resultado está visível


async def test_tui_enter_na_busca_volta_foco_pro_tree(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_focus_search()
        await pilot.pause()
        assert app.query_one("#search", Input).has_focus
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#tree", Tree).has_focus


async def test_tui_esc_limpa_busca(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        app.action_focus_search()
        await pilot.pause()
        search.value = "conserta"
        await _wait_until(pilot, lambda: len(_session_titles(app)) == 1)
        await pilot.press("escape")
        await pilot.pause()
        assert search.value == ""
        assert app.query_one("#tree", Tree).has_focus
        # rebuild debounced: as duas sessões voltam
        await _wait_until(pilot, lambda: len(_session_titles(app)) == 2)
        assert len(_session_titles(app)) == 2


async def test_tui_m_marca_e_desmarca_sessao(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """m marca a sessão atual (✓ no label) para o backup; m de novo desfaz."""
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = db.find_sessions(SID)[0]

        await pilot.press("m")
        await pilot.pause()
        assert app._picked == {("claude", SID)}
        tree = app.query_one("#tree", Tree)
        labels = [
            str(child.label)
            for node in tree.root.children
            for wt in node.children
            for child in wt.children
            if child.data and getattr(child.data, "kind", "") == "session"
        ]
        assert any(lbl.startswith("✓") and "conserta scanner" in lbl for lbl in labels)

        await pilot.press("m")
        await pilot.pause()
        assert app._picked == set()
        labels = [
            str(child.label)
            for node in tree.root.children
            for wt in node.children
            for child in wt.children
            if child.data and getattr(child.data, "kind", "") == "session"
        ]
        assert not any(lbl.startswith("✓") for lbl in labels)


async def test_tui_backup_modal_confirma_e_grava(
    db: Database, tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """b abre o modal (destino pré-preenchido do config); confirmar chama o
    backup com as sessões marcadas e registra o evento."""
    _fill_db(db, tmp_path)
    calls: list[tuple[list[dict], Path]] = []

    def fake_backup(rows, dest, now=None):  # type: ignore[no-untyped-def]
        calls.append((list(rows), Path(dest)))
        return BackupResult(
            folder=Path(dest) / "2026-11-07_15-30",
            done=list(rows),
            files=[Path("x.md")],
            errors=[],
        )

    monkeypatch.setattr("sessionhub.tui.backup_sessions", fake_backup)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._picked = {("claude", SID), ("claude", "99999999-2222-3333-4444-555555555555")}
        app.action_backup()
        await pilot.pause()
        assert isinstance(app.screen, BackupScreen)
        modal = app.screen
        # destino pré-preenchido com o [backup] dir do config
        assert modal.query_one("#backup-dest", Input).value == str(config.backup_dir)

        from textual.widgets import Button

        modal.query_one("#backup-confirm", Button).press()
        await _wait_until(pilot, lambda: len(calls) == 1)
        rows, dest = calls[0]
        assert {r["session_id"] for r in rows} == {SID, "99999999-2222-3333-4444-555555555555"}
        assert dest == Path(str(config.backup_dir))
        # evento Backup registrado (gravado após o to_thread retornar)
        await _wait_until(
            pilot, lambda: [e["event"] for e in db.last_events("claude", SID)] == ["Backup"]
        )
        events = db.last_events("claude", SID)
        assert [e["event"] for e in events] == ["Backup"]


async def test_tui_backup_sem_sessao_nao_abre_modal(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)  # banco com sessões, mas nada marcado/selecionado
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = None
        app.action_backup()
        await pilot.pause()
        assert not isinstance(app.screen, BackupScreen)


def _insert_com_transcript(db: Database, tp: Path) -> None:
    """Sessão cujo transcript existe de verdade (nome = <sid>.jsonl)."""
    from datetime import datetime

    db.upsert_sessions(
        [
            Session(
                harness="claude",
                session_id=SID,
                title="conserta scanner",
                cwd=str(tp.parent),
                repo_root=str(tp.parent),
                worktree_path=str(tp.parent),
                branch="main",
                last_activity=datetime.now(UTC),
                message_count=4,
                transcript_path=str(tp),
                resume_command=f"claude --resume {SID}",
                status="waiting",
            )
        ]
    )


async def test_tui_delete_modal_confirma_e_apaga(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """D abre o modal vermelho com os caminhos exatos; confirmar apaga o
    transcript do disco, a linha do índice e deixa evento + backup .md."""
    from textual.widgets import Button, Checkbox, Label

    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "G--dev-north"
    proj.mkdir(parents=True)
    tp = proj / f"{SID}.jsonl"
    tp.write_text(
        '{"type":"user","message":{"role":"user","content":"oi"}}\n', encoding="utf-8"
    )
    _insert_com_transcript(db, tp)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = db.find_sessions(SID)[0]

        await pilot.press("D")
        await pilot.pause()
        assert isinstance(app.screen, DeleteScreen)
        modal = app.screen
        assert tp.exists()  # nada é apagado antes de confirmar
        assert modal.paths == [tp]  # só o transcript desta sessão
        assert str(tp) in str(modal.query_one("#delete-paths", Label).content)
        assert modal.query_one("#delete-backup", Checkbox).value is True
        assert len(app.screen_stack) == 2  # default + modal

        await pilot.press("D")  # modal aberto: D não empilha outro
        await pilot.pause()
        assert len(app.screen_stack) == 2

        modal.query_one("#delete-confirm", Button).press()
        # poll: arquivo apagado + linha do índice + evento (nessa ordem)
        await _wait_until(pilot, lambda: db.find_sessions(SID) == [])

        assert not tp.exists()  # arquivo REAL apagado
        events = db.last_events("claude", SID)
        assert [e["event"] for e in events] == ["Excluída"]
        assert list(config.backup_dir.rglob("*.md"))  # backup .md gravado antes
        tree = app.query_one("#tree", Tree)
        assert app._find_session_node(tree.root, ("claude", SID)) is None


async def test_tui_delete_cancelado_nao_apaga(
    db: Database, tmp_path: Path, config: Config
) -> None:
    from textual.widgets import Button

    tp = tmp_path / f"{SID}.jsonl"
    tp.write_text("{}\n", encoding="utf-8")
    _insert_com_transcript(db, tp)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = db.find_sessions(SID)[0]
        await pilot.press("D")
        await pilot.pause()
        assert isinstance(app.screen, DeleteScreen)
        app.screen.query_one("#delete-cancel", Button).press()
        await pilot.pause()
        assert not isinstance(app.screen, DeleteScreen)
        assert tp.exists()  # cancelar não apaga nada
        assert len(db.find_sessions(SID)) == 1


async def test_tui_delete_sem_sessao_nao_abre_modal(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = None
        app.action_delete_session()
        await pilot.pause()
        assert not isinstance(app.screen, DeleteScreen)


async def test_tui_tecla_x_arquiva(db: Database, tmp_path: Path, config: Config) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
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
    app = _app(config, tmp_path)
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
    app = _app(config, tmp_path)
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
    app = _app(config, tmp_path)
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
    app = _app(config, tmp_path)
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
        await _wait_until(pilot, lambda: opened == [SID])  # abrir roda em thread
        assert opened == [SID]


async def test_tui_clique_unico_mostra_ultimas_mensagens(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """Clique único → preview abre nas ÚLTIMAS mensagens, com tamanho."""
    _fill_db(db, tmp_path)
    lines = [make_user_line(SID, str(tmp_path), "pedido inicial antigo")]
    for i in range(25):  # o tail guarda 25 — a primeira cai fora
        lines.append(make_user_line(SID, str(tmp_path), f"enchimento {i}"))
    lines.append(make_user_line(SID, str(tmp_path), "última pergunta", ts="2026-11-01T11:00:00Z"))
    lines.append(make_assistant_line(SID, str(tmp_path), "última resposta", "2026-11-01T11:00:30Z"))
    write_transcript(tmp_path / "t.jsonl", lines)
    app = _app(config, tmp_path)
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
        assert target is not None

        await tree._on_click(_StubClick(target, 1))  # clique único
        await _wait_until(pilot, lambda: bool(app._preview_cache))  # debounce
        cached = next(iter(app._preview_cache.values()), "")
        assert "última resposta" in cached
        assert "última pergunta" in cached
        assert "**tamanho:**" in cached  # tamanho da sessão no cabeçalho
        # pedido inicial continua lá — mas DEPOIS das últimas mensagens
        assert cached.index("Últimas mensagens") < cached.index("Pedido inicial")
        assert "pedido inicial antigo" in cached


async def test_tui_nota_esc_fecha_modal(
    db: Database, tmp_path: Path, config: Config
) -> None:
    """Esc fecha o modal de nota (bug 7 da revisão)."""
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = db.find_sessions(SID)[0]
        app.action_note()
        await pilot.pause()
        assert isinstance(app.screen, NoteScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NoteScreen)
        # nada foi salvo ao cancelar
        assert db.find_sessions(SID)[0]["note"] == ""


async def test_tui_nota_botao_salva(
    db: Database, tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_row = db.find_sessions(SID)[0]
        app.action_note()
        await pilot.pause()
        assert isinstance(app.screen, NoteScreen)
        modal = app.screen
        note_input = modal.query_one("#note-input", Input)
        note_input.value = "nota via botão"
        from textual.widgets import Button

        button = modal.query_one("#note-save", Button)
        button.press()  # type: ignore[attr-defined]
        await pilot.pause()
        await pilot.pause()
        assert db.find_sessions(SID)[0]["note"] == "nota via botão"


async def test_tui_preview_cacheado(
    db: Database, tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview em @work com cache (path, mtime): mesma sessão não relê."""
    _fill_db(db, tmp_path)
    calls: list[int] = []

    def fake_preview(row, worktree=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        return "md"

    monkeypatch.setattr("sessionhub.tui.preview_markdown", fake_preview)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = db.find_sessions(SID)[0]
        app._update_preview(row)
        await _wait_until(pilot, lambda: len(calls) == 1)
        app._update_preview(dict(row))  # mesma sessão
        await _wait_until(
            pilot,
            lambda: app._preview_task is not None and app._preview_task.done(),
        )
        assert len(calls) == 1  # cacheou
        # transcript diferente (path) → relê
        row2 = dict(row)
        row2["transcript_path"] = str(tmp_path / "outro.jsonl")
        app._update_preview(row2)
        await _wait_until(pilot, lambda: len(calls) == 2)


async def test_tui_rebuild_preserva_colapso(
    db: Database, tmp_path: Path, config: Config
) -> None:
    _fill_db(db, tmp_path)
    app = _app(config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", SessionTree)
        # colapsa o nó de worktree
        wt_node = tree.root.children[0].children[0]
        wt_node.collapse()
        label = str(wt_node.label)
        app._rebuild()
        await pilot.pause()
        wt_node2 = tree.root.children[0].children[0]
        assert not wt_node2.is_expanded
        assert str(wt_node2.label) == label
