"""CLI do SessionHub (Typer).

Sem subcomando: abre a TUI. Comandos: scan, ls, show, open, doctor.
"""

from __future__ import annotations

import json as _json
import os
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .config import load_config
from .db import Database
from .log import get_logger, setup_logging
from .models import STATUS_ICONS
from .open import open_session
from .preview import first_user_message, tail_messages
from .scan import run_scan
from .terminals import detect

log = get_logger(__name__)
console = Console()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="SessionHub — sessões de agentes de código, num lugar só.",
)

_STATUS_CHOICES = ["running", "waiting", "stopped", "forgotten", "unknown"]


def main() -> None:
    """Entry point do console script."""
    app()


# ------------------------------------------------------------------ helpers


def _fmt_age(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}min"
    if secs < 86400:
        return f"{secs // 3600}h"
    if secs < 7 * 86400:
        return f"{secs // 86400}d"
    return f"{secs // (7 * 86400)}sem"


def _git_badges(row: dict[str, Any]) -> str:
    parts: list[str] = []
    dirty = (
        (row.get("wt_staged") or 0) + (row.get("wt_unstaged") or 0) + (row.get("wt_untracked") or 0)
    )
    if dirty:
        parts.append(f"✎{dirty}")
    if row.get("wt_ahead"):
        parts.append(f"↑{row['wt_ahead']}")
    if row.get("wt_behind"):
        parts.append(f"↓{row['wt_behind']}")
    if row.get("wt_pr_number"):
        parts.append(f"PR#{row['wt_pr_number']}")
    return " ".join(parts) if parts else "·"


def _display_title(row: dict[str, Any]) -> str:
    title = row.get("title")
    if not title:
        base = row.get("cwd") or "?"
        return f"(sem título) · {os.path.basename(base) or base}"
    return title if len(title) <= 60 else title[:57] + "…"


def _session_line(row: dict[str, Any]) -> str:
    icon = STATUS_ICONS.get(row.get("status") or "", "❔")
    tail = row.get("worktree_path") or row.get("cwd") or ""
    tail = os.path.basename(tail) if tail else ""
    return (
        f"{icon} {row['harness']:<7} {_display_title(row):<60} "
        f"{(row.get('branch') or '—'):<18} {tail:<24} "
        f"{_git_badges(row):<14} {_fmt_age(row.get('last_activity'))}"
    )


def _print_summary(counts: dict[str, int], total: int) -> None:
    running = counts.get("running", 0)
    waiting = counts.get("waiting", 0)
    forgotten = counts.get("forgotten", 0)
    console.print(
        f"[bold]{running} rodando[/bold] · [yellow]{waiting} esperando você[/yellow] · "
        f"[red]{forgotten} esquecidas[/red] · {total} sessões"
    )


# ------------------------------------------------------------------ comandos


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    """SessionHub — sessões de agentes de código, num lugar só."""
    if ctx.invoked_subcommand is not None:
        return
    from .tui import SessionHubApp  # import tardio: textual só entra na TUI

    SessionHubApp().run()


@app.command()
def scan(
    full: bool = typer.Option(
        False, "--full", help="Reler todos os transcripts (ignora o índice)."
    ),
) -> None:
    """Atualiza o índice (scanners + git worktrees)."""
    config = load_config()
    setup_logging(config.log_dir)
    db = Database(config.db_path)
    try:
        stats = run_scan(db, config, full=full)
    finally:
        db.close()
    console.print(f"[green]✓[/green] scan em {stats.duration:.2f}s")
    console.print(f"  harnesses: {', '.join(stats.harnesses) or '—'}")
    console.print(
        f"  sessões novas/alteradas: {stats.sessions} · inalteradas: "
        f"{stats.sessions_skipped} · erros: {stats.errors}"
    )
    console.print(f"  repos: {stats.repos} · worktrees: {stats.worktrees}")


@app.command(name="ls")
def ls_(
    status: str = typer.Option(None, "--status", help="running|waiting|stopped|forgotten|unknown"),
    harness: str = typer.Option(None, "--harness", help="Filtrar por harness."),
    json_output: bool = typer.Option(False, "--json", help="Saída JSON (scripts/status line)."),
    limit: int = typer.Option(50, "--limit", "-n", help="Máximo de linhas (0 = todas)."),
    all_: bool = typer.Option(False, "--all", help="Incluir sessões arquivadas (ocultas)."),
) -> None:
    """Lista sessões do índice (mais recentes primeiro)."""
    if status is not None and status not in _STATUS_CHOICES:
        typer.secho(f"status inválido: {status} (use {', '.join(_STATUS_CHOICES)})", fg="red")
        raise typer.Exit(2)
    config = load_config()
    db = Database(config.db_path)
    try:
        rows = db.list_sessions(
            status=status, harness=harness, include_hidden=all_, limit=limit or None
        )
        counts = db.status_counts()
        total = db.counts()[0]
    finally:
        db.close()

    if json_output:
        for row in rows:
            row["worktree_exists"] = bool(row.get("worktree_path")) and os.path.isdir(
                row["worktree_path"]
            )
        typer.echo(_json.dumps(rows, ensure_ascii=False))
        return

    if not rows:
        console.print("[dim]nenhuma sessão no índice — rode `sessionhub scan` primeiro[/dim]")
        return
    _print_summary(counts, total)
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col, kw in (
        ("", {"width": 2, "no_wrap": True}),
        ("harness", {"min_width": 7}),
        ("sessão", {"min_width": 40, "overflow": "ellipsis", "no_wrap": True}),
        ("branch", {"min_width": 16}),
        ("worktree", {"min_width": 16}),
        ("git", {"min_width": 10}),
        ("idade", {"min_width": 6, "justify": "right"}),
    ):
        table.add_column(col, **kw)
    for row in rows:
        icon = STATUS_ICONS.get(row.get("status") or "", "❔")
        tail = row.get("worktree_path") or row.get("cwd") or ""
        tail = os.path.basename(tail) if tail else ""
        if row.get("hidden"):
            icon = "📦"
        note_mark = " 📌" if row.get("note") else ""
        table.add_row(
            icon,
            row["harness"],
            _display_title(row) + note_mark,
            row.get("branch") or "—",
            tail,
            _git_badges(row),
            _fmt_age(row.get("last_activity")),
        )
    console.print(table)


@app.command(name="open")
def open_(
    prefix: str = typer.Argument(..., help="ID da sessão (inteiro ou prefixo)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Só mostrar o comando, não executar."),
    repo: bool = typer.Option(False, "--repo", help="Abrir no repo principal (worktree sumiu)."),
    copy: bool = typer.Option(False, "--copy", help="Só copiar/imprimir o comando de retomada."),
) -> None:
    """Abre a sessão num terminal novo, no worktree dela."""
    config = load_config()
    db = Database(config.db_path)
    try:
        rows = db.find_sessions(prefix)
    finally:
        db.close()
    if not rows:
        typer.secho(f"nenhuma sessão com prefixo '{prefix}'", fg="red")
        raise typer.Exit(1)
    if len(rows) > 1:
        typer.secho(f"prefixo ambíguo ({len(rows)} sessões):", fg="yellow")
        for row in rows[:10]:
            typer.echo("  " + _session_line(row))
        raise typer.Exit(2)

    row = rows[0]
    cwd_override: str | None = None
    if repo and row.get("repo_root"):
        cwd_override = row["repo_root"]
    result = open_session(row, config, cwd_override=cwd_override, dry_run=dry_run or copy)
    if result.status == "missing":
        typer.secho(f"⚠ {result.message}", fg="yellow")
        typer.echo(f"  retomar: {result.command}")
        typer.echo("  opções: --repo (abrir no repo principal) · --copy (só o comando)")
        raise typer.Exit(0)
    if result.status in ("error", "dry_run"):
        color = "red" if result.status == "error" else "cyan"
        typer.secho(result.message, fg=color)
        raise typer.Exit(0 if result.status == "dry_run" else 1)
    typer.secho(f"🚀 abriu: {_display_title(row)}", fg="green")
    typer.echo(f"  {result.command}")


def _print_history(row: dict[str, Any], n: int) -> None:
    """Imprime o pedido inicial + as últimas mensagens da sessão."""
    icon = STATUS_ICONS.get(row.get("status") or "", "❔")
    console.print(f"[bold]{icon} {_display_title(row)}[/bold]")
    sid = str(row.get("session_id"))
    console.print(
        f"[dim]{row.get('harness')} · {sid[:13]}… · "
        f"{_fmt_age(row.get('last_activity'))} atrás · {row.get('message_count', 0)} msgs[/dim]"
    )
    if row.get("branch"):
        console.print(f"[dim]branch: {row['branch']}[/dim]")
    wt = row.get("worktree_path") or row.get("cwd") or ""
    if wt:
        mark = "" if os.path.isdir(str(wt)) else " [yellow]⚠ caminho não existe mais[/yellow]"
        console.print(f"[dim]{escape(wt)}{mark}[/dim]")
    if row.get("note"):
        console.print(f"[dim]📌 nota: {escape(str(row['note']))}[/dim]")
    console.print(f"[cyan]{escape(str(row.get('resume_command') or ''))}[/cyan]")
    console.print()

    messages = tail_messages(row.get("transcript_path"), str(row.get("harness")), max_messages=n)
    first = first_user_message(row.get("transcript_path"), str(row.get("harness")))
    if first and not (messages and messages[0].text == first.text):
        when = f" [dim]({first.ts})[/dim]" if first.ts else ""
        console.print(f"[bold cyan]👤 você — pedido inicial[/bold cyan]{when}")
        console.print(escape(first.text))
        console.print()
    if messages:
        console.print(
            f"[bold]últimas {len(messages)} mensagens[/bold] [dim](ordem cronológica)[/dim]"
        )
        for m in reversed(messages):
            who = "[bold cyan]👤 você[/bold cyan]" if m.role == "user" else "[dim]🤖 agente[/dim]"
            when = f" [dim]({m.ts})[/dim]" if m.ts else ""
            console.print(f"{who}{when}")
            console.print(escape(m.text))
            console.print()
    elif not first:
        console.print("[dim]transcript vazio, ilegível ou de harness ainda sem suporte.[/dim]")


@app.command(name="show")
def show(
    prefix: str = typer.Argument(..., help="ID da sessão (inteiro ou prefixo)."),
    messages: int = typer.Option(
        15, "--messages", "-n", help="Quantas mensagens do final (0 = só o pedido inicial)."
    ),
) -> None:
    """Mostra o que foi digitado: pedido inicial + últimas mensagens."""
    config = load_config()
    db = Database(config.db_path)
    try:
        rows = db.find_sessions(prefix)
    finally:
        db.close()
    if not rows:
        typer.secho(f"nenhuma sessão com prefixo '{prefix}'", fg="red")
        raise typer.Exit(1)
    if len(rows) > 1:
        typer.secho(f"prefixo ambíguo ({len(rows)} sessões):", fg="yellow")
        for row in rows[:10]:
            typer.echo("  " + _session_line(row))
        raise typer.Exit(2)
    _print_history(rows[0], messages)


@app.command()
def doctor() -> None:
    """Diagnóstico: harnesses, caminhos, git, terminal, problemas."""
    user_home = Path.home()
    config = load_config()
    term = detect()
    problems: list[str] = []
    notes: list[str] = []

    console.print(
        f"[bold]SessionHub {__version__}[/bold] · python {sys.version.split()[0]} · "
        f"{platform.system()} {platform.release()}"
    )
    console.print(f"config: {config.config_path}")
    console.print(f"dados : {config.home}")

    db = Database(config.db_path)
    try:
        total, wt_total = db.counts()
        last_scan = db.get_meta("last_scan")
        counts = db.status_counts()
    finally:
        db.close()
    console.print(
        f"índice: {total} sessões · {wt_total} worktrees · último scan {_fmt_age(last_scan)} atrás"
        if last_scan
        else f"índice: {total} sessões · {wt_total} worktrees · [red]nunca escaneado[/red]"
    )
    if total == 0:
        problems.append("índice vazio — rode `sessionhub scan`")

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("harness", min_width=10)
    table.add_column("detectado")
    table.add_column("caminho", overflow="ellipsis")
    table.add_column("arquivos", justify="right")

    claude_dir = user_home / ".claude" / "projects"
    n = sum(1 for _ in claude_dir.rglob("*.jsonl")) if claude_dir.is_dir() else 0
    table.add_row("claude", "sim" if claude_dir.is_dir() else "não", str(claude_dir), str(n))
    if claude_dir.is_dir() and n == 0:
        problems.append("claude: projects existe mas sem transcripts")

    codex_dir = user_home / ".codex" / "sessions"
    nz = sum(1 for _ in codex_dir.rglob("rollout-*")) if codex_dir.is_dir() else 0
    table.add_row("codex", "sim" if codex_dir.is_dir() else "não", str(codex_dir), str(nz))
    if codex_dir.is_dir() and nz:
        zst = sum(1 for p in codex_dir.rglob("rollout-*") if p.suffix == ".zst")
        if zst:
            notes.append(f"codex: {zst} rollouts comprimidos (.jsonl.zst) — suporte na fase 5")
        else:
            notes.append("codex: scanner planejado (fase 5)")

    gemini_dir = user_home / ".gemini"
    table.add_row("gemini", "sim" if gemini_dir.is_dir() else "não", str(gemini_dir), "—")
    if not gemini_dir.is_dir():
        notes.append("gemini: não instalado (scanner na fase 5)")

    aider_bin = shutil.which("aider")
    table.add_row("aider", "sim" if aider_bin else "não", aider_bin or "—", "—")
    if not aider_bin:
        notes.append("aider: não instalado (scanner na fase 5)")

    console.print(table)

    tools = [
        ("git", shutil.which("git")),
        ("gh", shutil.which("gh")),
        ("tmux", shutil.which("tmux")),
        ("wt.exe", shutil.which("wt.exe")),
        ("age", shutil.which("age")),
        ("code", shutil.which("code")),
    ]
    line = "  ".join(f"{'[green]✓[/green]' if p else '[dim]✗[/dim]'} {name}" for name, p in tools)
    console.print("ferramentas: " + line)
    if not shutil.which("age"):
        notes.append("age ausente — criptografia de backup (fase 4) ficará desativada")

    console.print(
        f"terminal   : {term.get('current') or '—'} "
        f"(tmux: {'sim' if term.get('inside_tmux') else 'não'} · "
        f"Windows Terminal: {'sim' if term.get('inside_windows_terminal') else 'não'})"
    )

    settings = user_home / ".claude" / "settings.json"
    hooks_state = "não instalados (fase 3)"
    if settings.exists():
        try:
            data = _json.loads(settings.read_text(encoding="utf-8"))
            hooks = data.get("hooks") if isinstance(data, dict) else None
            if hooks:
                hooks_state = "presentes (ver conteúdo)"
        except (OSError, ValueError):
            hooks_state = "ilegível"
    console.print(f"hooks claude: {hooks_state}")

    if counts:
        parts = ", ".join(f"{STATUS_ICONS.get(k, k)} {v} {k}" for k, v in sorted(counts.items()))
        console.print(f"status     : {parts}")

    if problems:
        console.print("[bold red]problemas:[/bold red]")
        for p in problems:
            console.print(f"  ✗ {p}")
    if notes:
        console.print("[dim]notas:[/dim]")
        for note in notes:
            console.print(f"  · {note}")
