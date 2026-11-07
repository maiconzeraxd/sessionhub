"""TUI do SessionHub (Textual).

Árvore repositório → worktree/branch → sessões, ordenada por última
atividade, com ícone de status, badges de git e idade. Painel lateral de
pré-visualização (últimas mensagens + git + notas). Filtros por
abas/teclas 1-4, busca fuzzy ( / ), filtro por harness.

Atalhos: Enter abre a sessão no terminal; n nota; c copia comando de
retomada; o abre o worktree no editor; x arquiva/oculta; s escaneia;
? ajuda. Mouse: clique único navega (painel de preview), duplo-clique
abre. Sem daemon, refresh automático a cada 5s.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, Markdown, Select, Tree

from .config import Config, load_config
from .db import Database
from .log import get_logger
from .models import STATUS_ICONS
from .open import open_in_editor, open_session
from .preview import preview_markdown
from .scan import run_scan

log = get_logger(__name__)

FILTERS = {
    "active": ("Ativas", ("running", "waiting")),
    "waiting": ("Esperando você", ("waiting",)),
    "forgotten": ("Esquecidas", ("forgotten",)),
    "all": ("Todas", None),
}


@dataclass
class NodeData:
    kind: str  # "repo" | "worktree" | "session" | "placeholder"
    row: dict[str, Any] | None = None


class SessionTree(Tree[NodeData]):
    """Árvore com semântica de clique do SessionHub.

    Clique único: move o cursor (NodeHighlighted → preview painel direito).
    Duplo-clique: seleciona (NodeSelected → abre a sessão / expande).
    Enter: seleciona, igual ao duplo-clique.
    """

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if "line" not in meta:
            return
        line = meta["line"]
        if meta.get("toggle", False):
            node = self.get_node_at_line(line)
            if node is not None:
                self._toggle_node(node)
            return
        node = self.get_node_at_line(line)
        if event.chain >= 2:
            self.cursor_line = line
            if node is not None:
                self.post_message(self.NodeSelected(node))
            return
        self.cursor_line = line  # highlight → preview, sem abrir


def _fuzzy(query: str, text: str) -> bool:
    """Match de subsequência (a la fzf, versão simples)."""
    q = query.lower().strip()
    if not q:
        return True
    iterator = iter(text.lower())
    return all(ch in iterator for ch in q)


def _age(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = max(0, int((datetime.now(UTC) - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}min"
    if secs < 86400:
        return f"{secs // 3600}h"
    if secs < 7 * 86400:
        return f"{secs // 86400}d"
    return f"{secs // (7 * 86400)}sem"


def _git_badges(wt: dict[str, Any] | None) -> str:
    if not wt:
        return ""
    parts: list[str] = []
    dirty = (wt.get("staged") or 0) + (wt.get("unstaged") or 0) + (wt.get("untracked") or 0)
    if dirty:
        parts.append(f"✎{dirty}")
    if wt.get("ahead"):
        parts.append(f"↑{wt['ahead']}")
    if wt.get("behind"):
        parts.append(f"↓{wt['behind']}")
    if wt.get("pr_number"):
        parts.append(f"PR#{wt['pr_number']}")
    return " ".join(parts)


# ------------------------------------------------------------------ modais


class HelpScreen(ModalScreen[None]):
    """Ajuda (?)."""

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-box { width: 64; height: auto; border: thick $accent;
                background: $surface; padding: 1 2; }
    """

    HELP_TEXT = """\
[b]Navegação[/b]
  ↑/↓           mover · Enter abrir/expandir
  1..4          Ativas · Esperando você · Esquecidas · Todas
  /             busca fuzzy (títulos/branch/caminho)
  Tab           alternar foco (árvore ↔ busca)

[b]Mouse[/b]
  clique        selecionar (painel direito mostra o que foi digitado)
  duplo-clique  abrir a sessão no terminal
  ▸             expandir/colapsar worktree

[b]Sessões[/b]
  Enter         abrir no terminal (worktree + resume)
  n             editar nota/tags (só no app, nunca no transcript)
  c             copiar comando de retomada
  o             abrir worktree no editor ($EDITOR ou code)
  x             arquivar/ocultar da lista (x de novo desfaz)
  b             backup agora (fase 4)

[b]Índice[/b]
  s             escanear agora (scanners + git)
  r             recarregar do índice
  H             mostrar/ocultar arquivadas

  q             sair · ? esta ajuda
"""

    def compose(self) -> ComposeResult:
        from textual.widgets import Static

        yield Static(self.HELP_TEXT, id="help-box")

    def on_key(self, event: Any) -> None:  # qualquer tecla fecha (exc. tab)
        if event.key in ("tab", "shift+tab"):
            return
        if event.key in ("question_mark", "?", "escape", "q"):
            event.stop()
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class NoteScreen(ModalScreen[tuple[str, str] | None]):
    """Editar nota/tags de uma sessão (Enter salva · Esc cancela)."""

    DEFAULT_CSS = """
    NoteScreen { align: center middle; }
    #note-box { width: 64; border: thick $accent; background: $surface; padding: 1 2; }
    #note-box Input { width: 1fr; }
    """

    def __init__(self, note: str, tags: str) -> None:
        super().__init__()
        self._note = note
        self._tags = tags

    def compose(self) -> ComposeResult:
        with Vertical(id="note-box"):
            yield Label("Nota da sessão (Enter salva · Esc cancela)")
            yield Input(self._note, id="note-input", placeholder="nota livre…")
            yield Input(self._tags, id="tags-input", placeholder="tags separadas por vírgula")

    def on_mount(self) -> None:
        self.query_one("#note-input", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "note-input":
            self.query_one("#tags-input", Input).focus()
            return
        self._save()

    @on(Button.Pressed, "#note-save")
    def _save_pressed(self) -> None:
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        note = self.query_one("#note-input", Input).value
        tags = self.query_one("#tags-input", Input).value
        self.dismiss((note, tags))


class MissingScreen(ModalScreen[str | None]):
    """Worktree sumiu: abrir no repo principal, copiar comando ou cancelar."""

    DEFAULT_CSS = """
    MissingScreen { align: center middle; }
    #missing-box { width: 64; height: auto; border: thick $warning;
                   background: $surface; padding: 1 2; }
    #missing-box Button { margin: 0 1 0 0; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        wt = self.row.get("worktree_path") or self.row.get("cwd") or "?"
        with Vertical(id="missing-box"):
            yield Label(f"⚠ worktree não existe mais:\n[bold]{wt}[/bold]")
            yield Label(f"retomar: [cyan]{self.row.get('resume_command')}[/cyan]")
            with Horizontal():
                yield Button("Abrir no repo principal", id="missing-repo", variant="primary")
                yield Button("Copiar comando", id="missing-copy")
                yield Button("Cancelar", id="missing-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "missing-repo":
            self.dismiss("repo")
        elif event.button.id == "missing-copy":
            self.dismiss("copy")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------ app


class SessionHubApp(App[None]):
    """SessionHub — árvore de sessões + pré-visualização."""

    TITLE = "SessionHub"
    CSS = """
    #main { height: 1fr; }
    #summary { height: 1; padding: 0 1; background: $panel; }
    #filters { height: 3; }
    #filters Button { min-width: 16; }
    #filters Input { width: 1fr; }
    #filters Select { width: 22; }
    #body { height: 1fr; }
    #tree { width: 62%; border-right: heavy $border; }
    #preview { width: 1fr; padding: 0 1; }
    Button.tab-active { background: $accent 30%; }
    """

    BINDINGS = [
        Binding("q", "quit", "Sair"),
        Binding("slash", "focus_search", "Buscar", key_display="/"),
        Binding("question_mark", "help", "Ajuda", key_display="?"),
        Binding("1", "filter_active", "Ativas"),
        Binding("2", "filter_waiting", "Esperando"),
        Binding("3", "filter_forgotten", "Esquecidas"),
        Binding("4", "filter_all", "Todas"),
        Binding("s", "scan", "Scan"),
        Binding("r", "refresh", "Recarregar"),
        Binding("c", "copy_resume", "Copiar cmd"),
        Binding("o", "open_editor", "Editor"),
        Binding("n", "note", "Nota"),
        Binding("x", "hide", "Arquivar"),
        Binding("b", "backup", "Backup"),
        Binding("H", "toggle_hidden_view", "Ocultas"),
    ]

    def __init__(self, config: Config | None = None, user_home: Path | None = None) -> None:
        super().__init__()
        self.config = config or load_config()
        self.user_home = user_home
        self._db = Database(self.config.db_path)
        self.filter_mode = "all"
        self.search = ""
        self.harness_filter: str | None = None
        self.show_hidden = False
        self._current_row: dict[str, Any] | None = None
        self._worktrees: dict[str, dict[str, Any]] = {}
        self._last_db_key: tuple[Any, ...] | None = None

    # ------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            yield Label("", id="summary")
            with Horizontal(id="filters"):
                yield Button("1 Ativas", id="tab-active")
                yield Button("2 Esperando", id="tab-waiting")
                yield Button("3 Esquecidas", id="tab-forgotten")
                yield Button("4 Todas", id="tab-all")
                yield Input(placeholder="buscar (/)", id="search")
                yield Select([], id="harness-select")
            with Horizontal(id="body"):
                yield SessionTree("SessionHub", id="tree")
                yield Markdown("", id="preview")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_harness_options()
        self._rebuild()
        self.set_interval(5.0, self._periodic_refresh)

    def on_unmount(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ dados

    def _db_state_key(self) -> tuple[Any, ...]:
        total, wt_total = self._db.counts()
        counts = tuple(sorted(self._db.status_counts().items()))
        return (total, wt_total, counts, self._db.get_meta("last_scan"))

    def _periodic_refresh(self) -> None:
        """Refresh leve: só reconstrói se o índice mudou (sem daemon)."""
        try:
            key = self._db_state_key()
        except Exception:  # banco fechado etc.
            return
        if key != self._last_db_key:
            self._rebuild()

    def _refresh_harness_options(self) -> None:
        sel = self.query_one("#harness-select", Select)
        harnesses = self._db.distinct_harnesses()
        options: list[tuple[str, str]] = [("todos", "")]
        options += [(h, h) for h in harnesses]
        sel.set_options(options)
        with contextlib.suppress(Exception):
            sel.value = "" if self.harness_filter is None else self.harness_filter

    def _row_matches(self, row: dict[str, Any]) -> bool:
        statuses = FILTERS[self.filter_mode][1]
        if statuses is not None and row.get("status") not in statuses:
            return False
        if self.harness_filter and row.get("harness") != self.harness_filter:
            return False
        if self.search:
            hay = " ".join(
                str(row.get(k) or "")
                for k in ("title", "session_id", "branch", "worktree_path", "cwd")
            )
            if not _fuzzy(self.search, hay):
                return False
        return True

    # ------------------------------------------------------------ árvore

    def _rebuild(self) -> None:
        tree = self.query_one("#tree", Tree)
        self._last_db_key = self._db_state_key()
        try:
            rows = self._db.list_sessions(include_hidden=self.show_hidden, limit=None)
        except Exception:
            rows = []
        self._worktrees = {w["path"]: w for w in self._db.list_worktrees()}
        sessions = [r for r in rows if self._row_matches(r)]

        selected: tuple[str, str] | None = None
        if self._current_row:
            selected = (self._current_row["harness"], self._current_row["session_id"])

        tree.clear()
        root = tree.root

        if not sessions and not self._worktrees:
            root.add(
                "índice vazio — aperte [bold]s[/bold] para escanear",
                data=NodeData("placeholder"),
                expand=False,
            )
            self._update_summary()
            return

        # agrupa sessões por worktree (fallback: cwd)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in sessions:
            key = row.get("worktree_path") or f"cwd::{row.get('cwd') or ''}"
            by_key.setdefault(key, []).append(row)

        # repos: das sessões + da tabela de worktrees (worktree sem sessão entra)
        repo_wt_keys: dict[str, set[str]] = {}
        repo_latest: dict[str, str] = {}
        for row in sessions:
            repo = row.get("repo_root") or "(sem repositório)"
            wt_key = row.get("worktree_path") or f"cwd::{row.get('cwd') or ''}"
            repo_wt_keys.setdefault(repo, set()).add(wt_key)
            ts = row.get("last_activity") or ""
            if ts > repo_latest.get(repo, ""):
                repo_latest[repo] = ts
        for wt in self._worktrees.values():
            repo = wt.get("repo_root") or "(sem repositório)"
            repo_wt_keys.setdefault(repo, set()).add(wt["path"])
            # worktrees sem sessão: usam scanned_at como atividade
            ts = wt.get("scanned_at") or ""
            if ts > repo_latest.get(repo, ""):
                repo_latest[repo] = ts

        def _wt_sort(key: str) -> str:
            rows = by_key.get(key, [])
            return max((r.get("last_activity") or "" for r in rows), default="")

        ordered = sorted(repo_wt_keys, key=lambda r: repo_latest.get(r, ""), reverse=True)
        for repo in ordered:
            repo_label = self._repo_label(repo, repo_wt_keys[repo])
            repo_node = root.add(repo_label, data=NodeData("repo"), expand=True)

            # ordena worktrees do repo pela sessão mais recente
            for wt_key in sorted(repo_wt_keys[repo], key=_wt_sort, reverse=True):
                wt_rows = by_key.get(wt_key, [])
                wt_node = repo_node.add(
                    self._worktree_label(wt_key, wt_rows),
                    data=NodeData("worktree"),
                    expand=True,
                )
                for row in sorted(
                    wt_rows, key=lambda r: r.get("last_activity") or "", reverse=True
                ):
                    wt_node.add(
                        self._session_label(row), data=NodeData("session", row), expand=False
                    )
        root.expand()

        if selected:
            node = self._find_session_node(root, selected)
            if node is not None:
                with contextlib.suppress(Exception):
                    tree.select_node(node)
        self._update_summary()

    def _find_session_node(self, node: Any, key: tuple[str, str]) -> Any:
        data = getattr(node, "data", None)
        if (
            isinstance(data, NodeData)
            and data.kind == "session"
            and data.row
            and (data.row["harness"], data.row["session_id"]) == key
        ):
            return node
        for child in getattr(node, "children", []):
            found = self._find_session_node(child, key)
            if found is not None:
                return found
        return None

    def _repo_label(self, repo: str, wt_keys: set[str]) -> str:
        if repo == "(sem repositório)":
            return f"📂 (sem repositório) · {len(wt_keys)} pastas"
        name = os.path.basename(repo) or repo
        drive = os.path.splitdrive(repo)[0]
        return f"📁 {name} ({drive}\\…) · {len(wt_keys)} worktrees"

    def _worktree_label(self, wt_key: str, rows: list[dict[str, Any]]) -> str:
        if wt_key.startswith("cwd::"):
            path = wt_key[5:]
            base = os.path.basename(path) or path or "?"
            return f"📂 {base} (fora de worktree)"
        wt = self._worktrees.get(wt_key)
        branch = (wt or {}).get("branch") or (rows[0].get("branch") if rows else None)
        badges = _git_badges(wt)
        if not os.path.isdir(wt_key):
            return f"⚠ {os.path.basename(wt_key)} · {branch or '—'} · caminho não existe mais"
        parts = [f"🌿 {branch or '—'}"]
        if badges:
            parts.append(badges)
        if not rows:
            parts.append("· sem sessões")
        return " ".join(parts) if parts else "🌿"

    def _session_label(self, row: dict[str, Any]) -> str:
        icon = STATUS_ICONS.get(row.get("status") or "", "❔")
        if row.get("hidden"):
            icon = "📦"
        title = row.get("title") or "(sem título)"
        if len(title) > 52:
            title = title[:49] + "…"
        note = " 📌" if row.get("note") else ""
        msgs = row.get("message_count") or 0
        age = _age(row.get("last_activity"))
        return f"{icon} {title} · {row['harness']} · {age} · {msgs}m{note}"

    def _update_summary(self) -> None:
        counts = self._db.status_counts()
        total = sum(counts.values())
        running = counts.get("running", 0)
        waiting = counts.get("waiting", 0)
        forgotten = len(
            {
                r["worktree_path"]
                for r in self._db.list_sessions(status="forgotten", include_hidden=True, limit=None)
                if r.get("worktree_path")
            }
        )
        label = (
            f"🟢 {running} rodando · 🟡 {waiting} esperando você · "
            f"🔴 {forgotten} worktrees esquecidas · {total} sessões"
        )
        if self.show_hidden:
            label += " · (mostrando ocultas)"
        self.query_one("#summary", Label).update(label)
        self._summary_text = label

    # ------------------------------------------------------------ eventos

    @on(Tree.NodeHighlighted)
    def _node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if isinstance(data, NodeData) and data.kind == "session" and data.row:
            self._current_row = data.row
            self._update_preview(data.row)
        elif isinstance(data, NodeData) and data.kind == "worktree":
            self._preview_worktree_hint()

    @on(Tree.NodeSelected)
    def _node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if isinstance(data, NodeData) and data.kind == "session" and data.row:
            self._open_row(data.row)

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.search = event.value
        self._rebuild()

    @on(Select.Changed, "#harness-select")
    def _harness_changed(self, event: Select.Changed) -> None:
        value = event.value
        self.harness_filter = value if isinstance(value, str) and value else None
        self._rebuild()

    @on(Button.Pressed, "#tab-active")
    def _tab_active(self) -> None:
        self._set_filter("active")

    @on(Button.Pressed, "#tab-waiting")
    def _tab_waiting(self) -> None:
        self._set_filter("waiting")

    @on(Button.Pressed, "#tab-forgotten")
    def _tab_forgotten(self) -> None:
        self._set_filter("forgotten")

    @on(Button.Pressed, "#tab-all")
    def _tab_all(self) -> None:
        self._set_filter("all")

    # ------------------------------------------------------------ ações

    def _set_filter(self, mode: str) -> None:
        self.filter_mode = mode
        for name in FILTERS:
            button = self.query_one(f"#tab-{name}", Button)
            button.set_class(name == mode, "tab-active")
        self._rebuild()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_filter_active(self) -> None:
        self._set_filter("active")

    def action_filter_waiting(self) -> None:
        self._set_filter("waiting")

    def action_filter_forgotten(self) -> None:
        self._set_filter("forgotten")

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_refresh(self) -> None:
        self._rebuild()
        self.notify("índice recarregado")

    def action_toggle_hidden_view(self) -> None:
        self.show_hidden = not self.show_hidden
        self._rebuild()
        state = "mostrando" if self.show_hidden else "ocultando"
        self.notify(f"arquivadas: {state}")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_backup(self) -> None:
        self.notify("backup das sessões chega na fase 4", severity="warning")

    def action_scan(self) -> None:
        asyncio.create_task(self._scan_task())

    async def _scan_task(self) -> None:
        self.notify("scan em andamento…")

        def _do_scan() -> tuple[int, int, float]:
            db = Database(self.config.db_path)
            try:
                stats = run_scan(db, self.config, user_home=self.user_home)
                return (stats.sessions, stats.sessions_skipped, stats.duration)
            finally:
                db.close()

        try:
            news, skipped, duration = await asyncio.to_thread(_do_scan)
        except Exception as exc:  # noqa: BLE001 - notificar qualquer falha
            log.exception("scan falhou")
            self.notify(f"scan falhou: {exc}", severity="error")
            return
        self._rebuild()
        self.notify(f"scan ✓ {news} novas · {skipped} inalteradas · {duration:.1f}s")

    def action_copy_resume(self) -> None:
        row = self._current_row
        if not row or not row.get("resume_command"):
            self.notify("selecione uma sessão primeiro", severity="warning")
            return
        text = str(row["resume_command"])
        try:
            self.copy_to_clipboard(text)
        except Exception:
            self.notify(f"copie manualmente: {text}", severity="warning")
            return
        self.notify(f"copiado: {text}")

    def action_open_editor(self) -> None:
        row = self._current_row
        if not row:
            self.notify("selecione uma sessão primeiro", severity="warning")
            return
        path = row.get("worktree_path") or row.get("cwd") or ""
        if not path:
            self.notify("sessão sem worktree", severity="warning")
            return
        result = open_in_editor(path, self.config)
        if result.status == "error":
            self.notify(result.message, severity="error")
        elif result.status == "missing":
            self.notify(result.message, severity="warning")
        else:
            self.notify(f"editor: {path}")

    def action_note(self) -> None:
        row = self._current_row
        if not row:
            self.notify("selecione uma sessão primeiro", severity="warning")
            return
        self.push_screen(
            NoteScreen(row.get("note") or "", row.get("tags") or ""), self._on_note_result
        )

    def _on_note_result(self, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        row = self._current_row
        if not row:
            return
        note, tags = result
        self._db.set_note(row["harness"], row["session_id"], note, tags)
        self.notify("nota salva (só no SessionHub)")
        self._rebuild()

    def action_hide(self) -> None:
        row = self._current_row
        if not row:
            self.notify("selecione uma sessão primeiro", severity="warning")
            return
        new_hidden = not bool(row.get("hidden"))
        self._db.set_hidden(row["harness"], row["session_id"], new_hidden)
        row["hidden"] = 1 if new_hidden else 0
        self.notify("arquivada (H para ver ocultas · x desfaz)" if new_hidden else "reexibida")
        self._rebuild()

    # ------------------------------------------------------------ abrir

    def _open_row(self, row: dict[str, Any]) -> None:
        path = row.get("worktree_path") or row.get("cwd")
        if not path or not os.path.isdir(path):
            if row.get("repo_root") and os.path.isdir(row["repo_root"]):
                self.push_screen(MissingScreen(row), self._on_missing_result)
            else:
                self.notify(
                    f"worktree sumiu — comando: {row.get('resume_command')}",
                    severity="warning",
                )
            return
        result = open_session(row, self.config)
        if result.status == "error":
            self.notify(result.message, severity="error")
            return
        title = row.get("title") or row["session_id"][:8]
        self.notify(f"🚀 abriu: {title}")

    def _on_missing_result(self, result: str | None) -> None:
        row = self._current_row
        if not row or result is None:
            return
        if result == "repo":
            opened = open_session(row, self.config, cwd_override=row.get("repo_root"))
            self.notify(
                f"abriu no repo: {opened.command}" if opened.status == "opened" else opened.message
            )
        elif result == "copy":
            text = str(row.get("resume_command") or "")
            try:
                self.copy_to_clipboard(text)
                self.notify(f"copiado: {text}")
            except Exception:
                self.notify(f"copie manualmente: {text}", severity="warning")

    # ------------------------------------------------------------ preview

    def _update_preview(self, row: dict[str, Any]) -> None:
        worktree = self._worktrees.get(row.get("worktree_path") or "")
        md = preview_markdown(row, worktree)
        self.query_one("#preview", Markdown).update(md)

    def _preview_worktree_hint(self) -> None:
        self.query_one("#preview", Markdown).update(
            "*selecione uma sessão para ver as últimas mensagens*"
        )
