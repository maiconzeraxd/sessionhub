"""TUI do SessionHub (Textual).

Árvore repositório → worktree/branch → sessões, ordenada por última
atividade, com ícone de status, badges de git e idade. Painel lateral de
pré-visualização (últimas mensagens + git + notas). Filtros por
abas/teclas 1-4, busca fuzzy ( / ), filtro por harness.

Atalhos: Enter abre a sessão no terminal; n nota; c copia comando de
retomada; o abre o worktree no editor; x arquiva/oculta; s escaneia;
? ajuda. Mouse: clique único navega (painel de preview), duplo-clique
abre. Refresh: watcher opcional (watchdog) dispara scan quente quando
transcripts mudam; sem ele, assinatura do índice a cada 5s.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import unicodedata
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

from .backup import BackupResult, backup_sessions
from .config import Config, load_config
from .db import Database
from .fmt import age, git_badges, human_size, short_path
from .log import get_logger
from .models import STATUS_ICONS
from .open import open_in_editor, open_session
from .preview import preview_markdown
from .scan import run_scan
from .watcher import TranscriptWatcher

log = get_logger(__name__)

FILTERS = {
    "active": ("Ativas", ("running", "waiting")),
    "waiting": ("Esperando você", ("waiting",)),
    "forgotten": ("Esquecidas", ("forgotten",)),
    "all": ("Todas", None),
}

_PREVIEW_DEBOUNCE = 0.15  # s — navegação rápida não relê transcript a cada seta
_SEARCH_DEBOUNCE = 0.2  # s — digitar não reconstroi a árvore por tecla
_PREVIEW_CACHE_MAX = 64


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


def _fold(text: str) -> str:
    """Minúsculas sem acento: 'Aplicação' → 'aplicacao'.

    A busca aceita digitar sem acento (teclado/layout), normalizando os
    dois lados (query e texto do índice).
    """
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _fuzzy(query: str, text: str) -> bool:
    """Match de subsequência (a la fzf, versão simples), sem acento."""
    q = _fold(query).strip()
    if not q:
        return True
    iterator = iter(_fold(text))
    return all(ch in iterator for ch in q)


def _search_matches(query: str, short: str, paths: str) -> bool:
    """Query casa por subsequência nos campos curtos (título, branch, id…)
    OU por substring nos caminhos (worktree, cwd, repo).

    Subsequência em caminho longo casa com quase qualquer query (as letras
    aparecem em sequência em algum lugar do caminho) — a busca "filtra"
    sem esconder nada. Por isso caminho só aceita substring.
    """
    if _fuzzy(query, short):
        return True
    q = _fold(query).strip()
    return bool(q) and q in _fold(paths)


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
  /             busca fuzzy (título/branch/caminho, sem acento)
  Enter         busca: volta o foco pra árvore (filtro continua)
  Esc           busca: limpa e volta pra árvore
  Tab           alternar foco (árvore ↔ busca)

[b]Mouse[/b]
  clique        selecionar (painel direito mostra o que foi digitado)
  duplo-clique  abrir a sessão no terminal
  ▸             expandir/colapsar worktree

[b]Sessões[/b]
  Enter         abrir no terminal (worktree + resume)
  n             editar nota/tags (só no app, nunca no transcript)
  m             marcar/desmarcar sessão para o backup (✓)
  b             backup: sessões marcadas → pasta datada, um .md por sessão
  c             copiar comando de retomada
  o             abrir worktree no editor ($EDITOR ou code)
  x             arquivar/ocultar da lista (x de novo desfaz)

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
    #note-actions { height: 3; align-horizontal: right; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, note: str, tags: str) -> None:
        super().__init__()
        self._note = note
        self._tags = tags

    def compose(self) -> ComposeResult:
        with Vertical(id="note-box"):
            yield Label("Nota da sessão (Enter salva · Esc cancela)")
            yield Input(self._note, id="note-input", placeholder="nota livre…")
            yield Input(self._tags, id="tags-input", placeholder="tags separadas por vírgula")
            with Horizontal(id="note-actions"):
                yield Button("Salvar", id="note-save", variant="primary")
                yield Button("Cancelar", id="note-cancel")

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

    @on(Button.Pressed, "#note-cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(None)

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


class BackupScreen(ModalScreen[dict[str, Any] | None]):
    """Confirmar backup: sessões marcadas → pasta datada, um .md por sessão.

    O destino é editável (pré-preenchido com o [backup] dir do config).
    Enter ou o botão confirmam; Esc cancela. Retorna
    {"rows": [...], "dest": caminho} ou None.
    """

    DEFAULT_CSS = """
    BackupScreen { align: center middle; }
    #backup-box { width: 72; height: auto; max-height: 80%;
                  border: thick $accent; background: $surface; padding: 1 2; }
    #backup-box Label { width: 1fr; }
    #backup-names { height: auto; max-height: 12; overflow-y: auto;
                    color: $text-muted; }
    #backup-dest { width: 1fr; }
    #backup-actions { height: 3; align-horizontal: right; }
    #backup-box Button { margin: 0 1 0 0; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    MAX_NAMES = 8

    def __init__(self, rows: list[dict[str, Any]], dest: str) -> None:
        super().__init__()
        self.rows = rows
        self._dest = dest

    def compose(self) -> ComposeResult:
        names = [str(r.get("title") or r.get("session_id")) for r in self.rows]
        shown = "\n".join(f"✓ {n}" for n in names[: self.MAX_NAMES])
        if len(names) > self.MAX_NAMES:
            shown += f"\n… (+{len(names) - self.MAX_NAMES} sessões)"
        with Vertical(id="backup-box"):
            yield Label(f"Backup de {len(self.rows)} sessão(ões) — um .md por sessão")
            yield Label(shown, id="backup-names")
            yield Label("destino (uma pasta com a data será criada dentro dele):")
            yield Input(self._dest, id="backup-dest")
            with Horizontal(id="backup-actions"):
                yield Button(
                    f"Fazer backup ({len(self.rows)})",
                    id="backup-confirm",
                    variant="primary",
                )
                yield Button("Cancelar", id="backup-cancel")

    def on_mount(self) -> None:
        self.query_one("#backup-dest", Input).focus()

    @on(Input.Submitted, "#backup-dest")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._confirm()

    @on(Button.Pressed, "#backup-confirm")
    def _confirm_pressed(self) -> None:
        self._confirm()

    @on(Button.Pressed, "#backup-cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _confirm(self) -> None:
        dest = self.query_one("#backup-dest", Input).value.strip() or self._dest
        self.dismiss({"rows": self.rows, "dest": dest})


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
        Binding("m", "toggle_pick", "Marcar"),
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
        self._picked: set[tuple[str, str]] = set()  # (harness, session_id) p/ backup
        self._current_row: dict[str, Any] | None = None
        self._worktrees: dict[str, dict[str, Any]] = {}
        self._last_db_key: tuple[Any, ...] | None = None
        # assíncrono: preview (debounce+cache), busca, watcher, abrir sessão
        self._preview_task: asyncio.Task[None] | None = None
        self._preview_cache: dict[tuple[str, float], str] = {}
        self._search_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._watcher: TranscriptWatcher | None = None

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
        self._start_watcher()

    def on_unmount(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        self._db.close()

    # ------------------------------------------------------------ watcher

    def _start_watcher(self) -> None:
        """Observa os diretórios de transcripts → scan quente em background.

        Sem watchdog instalado é um no-op (o refresh de 5s cobre). Fase 5:
        outros harnesses registram seus roots aqui.
        """
        home = self.user_home or Path.home()
        roots = [home / ".claude" / "projects"]
        watcher = TranscriptWatcher(self._on_transcripts_changed)
        if watcher.start(roots):
            self._watcher = watcher

    def _on_transcripts_changed(self) -> None:
        """Thread do observer → volta para o event loop da TUI."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon_threadsafe(self._schedule_watch_scan)

    def _schedule_watch_scan(self) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            return  # já agendado (rajada de eventos)
        self._watch_task = asyncio.create_task(self._watch_scan_task())

    async def _watch_scan_task(self) -> None:
        await asyncio.sleep(2.0)  # agrupa rajadas de escrita do transcript
        await self._scan_task(quiet=True)

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
            short = " ".join(
                str(row.get(k) or "")
                for k in ("title", "session_id", "branch", "note", "tags")
            )
            paths = " ".join(
                str(row.get(k) or "") for k in ("worktree_path", "cwd", "repo_root")
            )
            if not _search_matches(self.search, short, paths):
                return False
        return True

    def _wt_matches_search(self, wt: dict[str, Any]) -> bool:
        """Worktree sem sessões ainda aparece se o branch/caminho bate."""
        short = str(wt.get("branch") or "")
        paths = f"{wt.get('path') or ''} {wt.get('repo_root') or ''}"
        return _search_matches(self.search, short, paths)

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
        searching = bool(self.search.strip())

        selected: tuple[str, str] | None = None
        if self._current_row:
            selected = (self._current_row["harness"], self._current_row["session_id"])

        # preserva o estado visual: worktrees colapsadas + rolagem
        # (durante a busca NÃO preserva: resultados novos precisam aparecer,
        #  e o scroll da árvore completa não significa nada na filtrada)
        collapsed, scroll_y = self._tree_state(tree)
        tree.clear()
        root = tree.root

        if not rows and not self._worktrees:
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

        # repos: das sessões + da tabela de worktrees (worktree sem sessão entra).
        # Durante a busca o worktree sem sessão/branch/caminho correspondente
        # fica de fora — senão a árvore nunca estreia e a busca parece morta.
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
            if searching and not by_key.get(wt["path"]) and not self._wt_matches_search(wt):
                continue
            repo_wt_keys.setdefault(repo, set()).add(wt["path"])
            # worktrees sem sessão: usam o commit mais recente como atividade
            ts = wt.get("head_ts") or wt.get("scanned_at") or ""
            if ts > repo_latest.get(repo, ""):
                repo_latest[repo] = ts

        if not repo_wt_keys:
            root.add(
                f"nenhum resultado para [bold]{self.search.strip()}[/bold]"
                " — [bold]Esc[/bold] limpa a busca",
                data=NodeData("placeholder"),
                expand=False,
            )
            self._update_summary()
            return

        def _wt_sort(key: str) -> str:
            wt_rows = by_key.get(key, [])
            wt = self._worktrees.get(key)
            candidates = [r.get("last_activity") or "" for r in wt_rows]
            candidates.append(str(wt.get("head_ts") or "") if wt else "")
            return max(candidates, default="")

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

        if collapsed and not searching:
            for node in self._walk_nodes(root):
                if str(node.label) in collapsed:
                    with contextlib.suppress(Exception):
                        node.collapse()
        if scroll_y > 0 and not searching:
            with contextlib.suppress(Exception):
                tree.scroll_to(y=scroll_y, animate=False)

        if selected:
            node = self._find_session_node(root, selected)
            if node is not None:
                with contextlib.suppress(Exception):
                    # move_cursor SÓ destaca (NodeHighlighted → preview).
                    # select_node postaria NodeSelected → abriria a sessão num
                    # terminal novo a cada rebuild (bug: busca/filtro/nota
                    # re-abriam a sessão selecionada).
                    tree.move_cursor(node)
        self._update_summary()

    @staticmethod
    def _walk_nodes(node: Any) -> Any:
        for child in getattr(node, "children", []):
            yield child
            yield from SessionHubApp._walk_nodes(child)

    def _tree_state(self, tree: Tree) -> tuple[set[str], float]:
        """(rótulos colapsados, scroll_y) antes de um rebuild."""
        collapsed: set[str] = set()
        try:
            for node in self._walk_nodes(tree.root):
                if not node.is_expanded:
                    collapsed.add(str(node.label))
            scroll_y = float(tree.scroll_offset.y)
        except Exception:
            return set(), 0.0
        return collapsed, scroll_y

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
        return f"📁 {name} ({short_path(repo)}) · {len(wt_keys)} worktrees"

    def _worktree_label(self, wt_key: str, rows: list[dict[str, Any]]) -> str:
        if wt_key.startswith("cwd::"):
            path = wt_key[5:]
            base = os.path.basename(path) or path or "?"
            return f"📂 {base} (fora de worktree)"
        wt = self._worktrees.get(wt_key)
        branch = (wt or {}).get("branch") or (rows[0].get("branch") if rows else None)
        badges = git_badges(wt)
        if not os.path.isdir(wt_key):
            return f"⚠ {os.path.basename(wt_key)} · {branch or '—'} · caminho não existe mais"
        icon = "🔴" if (wt or {}).get("forgotten") else "🌿"
        parts = [f"{icon} {branch or '—'}"]
        if badges:
            parts.append(badges)
        if not rows:
            age_txt = age((wt or {}).get("head_ts"))
            hint = "esquecida" if (wt or {}).get("forgotten") else "sem sessões"
            parts.append(f"· {hint} · {age_txt}")
        return " ".join(parts) if parts else "🌿"

    def _session_label(self, row: dict[str, Any]) -> str:
        icon = STATUS_ICONS.get(row.get("status") or "", "❔")
        if row.get("hidden"):
            icon = "📦"
        mark = "✓ " if (row["harness"], row["session_id"]) in self._picked else ""
        title = row.get("title") or "(sem título)"
        if len(title) > 52:
            title = title[:49] + "…"
        note = " 📌" if row.get("note") else ""
        msgs = row.get("message_count") or 0
        age_txt = age(row.get("last_activity"))
        size = human_size(self._session_size(row))
        return (
            f"{mark}{icon} {title} · {row['harness']} · {age_txt}"
            f" · {msgs}m{note} · {size}"
        )

    @staticmethod
    def _session_size(row: dict[str, Any]) -> int | None:
        """Tamanho do transcript (None se sumiu) — stat barato por rebuild."""
        tp = row.get("transcript_path")
        if not tp:
            return None
        try:
            return os.path.getsize(str(tp))
        except OSError:
            return None

    def _update_summary(self) -> None:
        counts = self._db.status_counts()
        total = sum(counts.values())
        running = counts.get("running", 0)
        waiting = counts.get("waiting", 0)
        # consulta agregada (sem listar todas as sessões esquecidas)
        forgotten = self._db.forgotten_worktrees()
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
        if self._search_task is not None:
            self._search_task.cancel()
        self._search_task = asyncio.create_task(self._search_debounced())

    async def _search_debounced(self) -> None:
        try:
            await asyncio.sleep(_SEARCH_DEBOUNCE)
        except asyncio.CancelledError:
            return  # tecla nova chegou
        self._rebuild()

    @on(Input.Submitted, "#search")
    def _search_submitted(self, event: Input.Submitted) -> None:
        """Enter na busca: mantém o filtro e devolve o foco à árvore."""
        event.stop()
        self.query_one("#tree", Tree).focus()

    def on_key(self, event: events.Key) -> None:
        """Esc na caixa de busca: limpa o filtro e volta pra árvore.

        (O Input não consome Esc — o evento borbulha até aqui. Modais têm
        binding próprio de Esc e fecham antes de chegar neste handler.)
        """
        if event.key != "escape":
            return
        focused = self.focused
        if isinstance(focused, Input) and focused.id == "search":
            event.stop()
            event.prevent_default()
            focused.value = ""  # dispara Changed → rebuild (debounced)
            self.query_one("#tree", Tree).focus()

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

    def action_toggle_pick(self) -> None:
        """Marca/desmarca a sessão atual para o backup (m)."""
        row = self._current_row
        if not row:
            self.notify("selecione uma sessão primeiro", severity="warning")
            return
        key = (row["harness"], row["session_id"])
        if key in self._picked:
            self._picked.discard(key)
            self.notify("desmarcada")
        else:
            self._picked.add(key)
            self.notify("marcada para o backup (b confirma)")
        self._rebuild()

    def action_backup(self) -> None:
        """Backup das sessões marcadas (m) — ou da selecionada, se nenhuma."""
        rows = [
            r
            for r in self._db.list_sessions(include_hidden=True, limit=None)
            if (r["harness"], r["session_id"]) in self._picked
        ]
        if not rows and self._current_row:
            rows = [self._current_row]
        if not rows:
            self.notify(
                "marque sessões com m (ou navegue até uma) antes do backup",
                severity="warning",
            )
            return
        self.push_screen(
            BackupScreen(rows, str(self.config.backup_dir)), self._on_backup_result
        )

    def _on_backup_result(self, result: dict[str, Any] | None) -> None:
        if not result:
            return  # cancelado
        rows = [r for r in result.get("rows") or [] if isinstance(r, dict)]
        dest = str(result.get("dest") or "")
        if rows and dest:
            asyncio.create_task(self._backup_task(rows, dest))

    async def _backup_task(self, rows: list[dict[str, Any]], dest: str) -> None:
        """Grava os .md fora do event loop; eventos + notificação no fim."""
        self.notify(f"backup de {len(rows)} sessão(ões)…")
        try:
            result = await asyncio.to_thread(backup_sessions, rows, Path(dest))
        except Exception as exc:  # noqa: BLE001 - backup não derruba a TUI
            log.exception("backup falhou")
            self.notify(f"backup falhou: {exc}", severity="error")
            return
        assert isinstance(result, BackupResult)
        ts = datetime.now(UTC).isoformat()
        for row in result.done:
            with contextlib.suppress(Exception):
                self._db.record_event(ts, row["harness"], row["session_id"], "Backup")
        msg = f"backup ✓ {len(result.files)} .md → {result.folder}"
        if result.errors:
            msg += f" · {len(result.errors)} falha(s)"
            self.notify("\n".join(result.errors[:3]), severity="warning")
        self.notify(msg)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_scan(self) -> None:
        asyncio.create_task(self._scan_task())

    async def _scan_task(self, quiet: bool = False) -> None:
        if not quiet:
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
        if not quiet:
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
        # fora do event loop: launchers "cli" podem levar até 15s (timeout)
        asyncio.create_task(self._open_row_async(row))

    async def _open_row_async(self, row: dict[str, Any]) -> None:
        try:
            result = await asyncio.to_thread(open_session, row, self.config)
        except Exception as exc:  # noqa: BLE001 - abrir nunca derruba a TUI
            log.exception("abrir sessão falhou")
            self.notify(f"falha ao abrir: {exc}", severity="error")
            return
        if result.status == "error":
            self.notify(result.message, severity="error")
            return
        title = row.get("title") or str(row["session_id"])[:8]
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
        """Preview com debounce: leitura do transcript roda fora do event
        loop (transcripts de GB não travam a TUI) e é cacheada por
        (caminho, mtime) — voltar para a mesma sessão não relê nada."""
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_task = asyncio.create_task(self._preview_task_run(row))

    async def _preview_task_run(self, row: dict[str, Any]) -> None:
        try:
            await asyncio.sleep(_PREVIEW_DEBOUNCE)
            tp = str(row.get("transcript_path") or "")
            try:
                mtime = os.path.getmtime(tp) if tp else 0.0
            except OSError:
                mtime = 0.0
            key = (tp, mtime)
            md = self._preview_cache.get(key)
            if md is None:
                worktree = self._worktrees.get(row.get("worktree_path") or "")
                md = await asyncio.to_thread(preview_markdown, row, worktree)
                if len(self._preview_cache) >= _PREVIEW_CACHE_MAX:
                    self._preview_cache.clear()
                self._preview_cache[key] = md
            self.query_one("#preview", Markdown).update(md)
        except asyncio.CancelledError:
            return  # substituído por um preview mais novo
        except Exception:  # noqa: BLE001 - preview falha não derruba a TUI
            log.exception("preview falhou")

    def _preview_worktree_hint(self) -> None:
        self.query_one("#preview", Markdown).update(
            "*selecione uma sessão para ver as últimas mensagens*"
        )
