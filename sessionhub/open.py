"""Abre uma sessão num terminal novo (foca pane do tmux na fase 3).

Ordem de escolha dos launchers (o primeiro que abrir de verdade ganha):
1. `open_command` do config.toml (template com {cwd} e {cmd} — o template é
   dividido ANTES de substituir, para sobreviver a caminhos com espaço);
2. `terminal` explícito do config ("wt", "tmux", "kitty", "wezterm", …);
3. o terminal em que o SessionHub está rodando agora (KITTY_WINDOW_ID /
   WEZTERM_PANE / TERM_PROGRAM) — comandos `cli` (kitty @, wezterm cli)
   só funcionam do terminal certo;
4. tmux, se estivermos dentro do $TMUX;
5. padrão da plataforma: Windows → wt.exe; macOS → iTerm2/Terminal/Ghostty/
   kitty/WezTerm; Linux → kitty/WezTerm/Alacritty/gnome-terminal/konsole.

Launchers "cli" (kitty @ / wezterm cli / tmux / osascript) falam com um
terminal que já existe: rodam com subprocess.run + timeout e o código de
saída é verificado — falhou, cai para o próximo candidato em vez de
"abrir sem abrir nada". Launchers "spawn" (GUI) são destacados e
fire-and-forget, mas um OSError ao lançar também desempata para o próximo.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Config
from .log import get_logger
from .terminals import current_terminal_program

log = get_logger(__name__)

CLI_TIMEOUT = 15.0  # kitty @/wezterm cli/tmux/osascript: limiar de "abriu?"

# session_id precisa ser um token de shell inofensivo antes de ser embutido
# em cmd /k, sh -c, tmux ou AppleScript (ids do Claude são UUIDs).
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@/\\-]{2,127}$")


@dataclass
class OpenResult:
    status: str  # "opened" | "missing" | "error" | "dry_run"
    command: str | None = None  # comando de retomada (ou argv montado, no dry_run)
    message: str = ""


@dataclass
class Launcher:
    """Um jeito de abrir o terminal. `kind`:

    - "cli": fala com um terminal existente (kitty @, wezterm cli, tmux,
      osascript) — verificado com subprocess.run + rc;
    - "spawn": lança um GUI novo (Popen destacado, fire-and-forget).
    """

    argv: list[str]
    kind: str = "spawn"
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.argv[0] if self.argv else "?"

    @property
    def display(self) -> str:
        return " ".join(self.argv)


def _split_template(rendered: str) -> list[str]:
    """Split respeitando aspas SEM comer barras invertidas do Windows."""
    tokens = shlex.split(rendered, posix=False)
    return [t.strip("\"'") for t in tokens]


def _render_template(template: str, cwd: str, cmd: str) -> list[str]:
    """Divide o template ANTES de substituir {cwd}/{cmd} dentro de cada
    argumento — substituir antes quebrava com espaço no caminho
    (vira dois argumentos) e com aspas no comando."""
    tokens = _split_template(template)
    return [t.replace("{cwd}", cwd).replace("{cmd}", cmd) for t in tokens]


def _sh_quote(text: str) -> str:
    return shlex.quote(text)


# ------------------------------------------------------------- builders POSIX


def _apple_inner(cwd: str, cmd: str) -> str:
    """Comando interno do AppleScript: `sh -c 'cd … && cmd'`.

    Funciona tanto se o app executa a string via shell (Terminal) quanto
    direto (iTerm): o sh -c explícito garante o && em ambos.
    """
    return f"sh -c {shlex.quote(f'cd {cwd} && {cmd}')}"


def _applescript_literal(text: str) -> str:
    """Escapa para dentro de um literal de string AppleScript (\\ e ")."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _osascript_argv(app: str, action: str, cwd: str, cmd: str) -> list[str]:
    literal = _applescript_literal(_apple_inner(cwd, cmd))
    script = f'tell application "{app}" to {action} "{literal}"'
    return ["osascript", "-e", script]


def _kitty_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("kitty"):
        return None
    tokens = shlex.split(cmd)
    if os.environ.get("KITTY_WINDOW_ID"):
        # dentro do kitty: janela no MESMO kitty (remote control)
        return Launcher(
            ["kitty", "@", "launch", "--type", "tab", "--cwd", cwd, *tokens],
            "cli",
            "kitty @",
        )
    return Launcher(["kitty", "--directory", cwd, *tokens], "spawn", "kitty")


def _wezterm_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("wezterm"):
        return None
    tokens = shlex.split(cmd)
    if os.environ.get("WEZTERM_PANE"):
        # dentro do wezterm: pane novo no MESMO wezterm
        return Launcher(
            ["wezterm", "cli", "spawn", "--cwd", cwd, "--", *tokens],
            "cli",
            "wezterm cli",
        )
    return Launcher(
        ["wezterm", "start", "--always-new-process", "--cwd", cwd, "--", *tokens],
        "spawn",
        "wezterm start",
    )


def _ghostty_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("ghostty"):
        return None
    tokens = shlex.split(cmd)
    return Launcher(["ghostty", "--working-directory", cwd, "-e", *tokens], "spawn", "ghostty")


def _tmux_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("tmux"):
        return None
    return Launcher(["tmux", "new-window", "-c", cwd, cmd], "cli", "tmux")


def _wt_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("wt.exe"):
        return None
    return Launcher(["wt.exe", "-d", cwd, "cmd", "/k", cmd], "spawn", "wt")


def _sh_c_launcher(binary: str, *prefix: str) -> Callable[[str, str], Launcher | None]:
    """Builder genérico: <binary> <prefix…> sh -c 'cd … && cmd'."""

    def build(cwd: str, cmd: str) -> Launcher | None:
        if not shutil.which(binary):
            return None
        return Launcher(
            [*prefix, "sh", "-c", f"cd {_sh_quote(cwd)} && {cmd}"],
            "spawn",
            binary,
        )

    return build


_alacritty = _sh_c_launcher("alacritty", "-e")
_gnome = _sh_c_launcher("gnome-terminal", "--")
_konsole = _sh_c_launcher("konsole", "-e")
_xterm = _sh_c_launcher("x-terminal-emulator", "-e")


def _iterm_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("osascript"):
        return None
    argv = _osascript_argv(
        "iTerm", "create window with default profile command", cwd, cmd
    )
    return Launcher(argv, "cli", "iTerm (osascript)")


def _terminal_app_launcher(cwd: str, cmd: str) -> Launcher | None:
    if not shutil.which("osascript"):
        return None
    argv = _osascript_argv("Terminal", "do script", cwd, cmd)
    return Launcher(argv, "cli", "Terminal.app (osascript)")


_BUILDERS: dict[str, Callable[[str, str], Launcher | None]] = {
    "wt": _wt_launcher,
    "tmux": _tmux_launcher,
    "kitty": _kitty_launcher,
    "wezterm": _wezterm_launcher,
    "ghostty": _ghostty_launcher,
    "alacritty": _alacritty,
    "gnome-terminal": _gnome,
    "gnome": _gnome,
    "konsole": _konsole,
    "x-terminal-emulator": _xterm,
    "xterm": _xterm,
    "iterm": _iterm_launcher,
    "iterm2": _iterm_launcher,
    "apple-terminal": _terminal_app_launcher,
    "terminal": _terminal_app_launcher,
}

# apelidos aceitos no config.terminal
_ALIASES = {
    "windows-terminal": "wt",
    "windowsterminal": "wt",
    "wt.exe": "wt",
    "iterm.app": "iterm",
    "apple_terminal": "apple-terminal",
}


def _platform_defaults() -> list[str]:
    if os.name == "nt":
        return ["wt"]
    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        return ["kitty", "ghostty", "wezterm", "iterm", "apple-terminal"]
    return [
        "kitty",
        "wezterm",
        "alacritty",
        "gnome-terminal",
        "konsole",
        "x-terminal-emulator",
    ]


def build_launchers(cwd: str, cmd: str, config: Config) -> list[Launcher]:
    """Launchers candidatos, em ordem de prioridade."""
    if config.open_command:
        argv = _render_template(config.open_command, cwd, cmd)
        return [Launcher(argv, "spawn", "open_command")]

    term = (config.terminal or "auto").strip().lower()
    term = _ALIASES.get(term, term)

    order: list[str] = []
    # 1. terminal explícito do config ("wt", "kitty", "tmux", …)
    if term and term != "auto" and term not in order:
        order.append(term)
    # 2. terminal em que estamos agora (kitty @/wezterm cli só funcionam daqui)
    hint = current_terminal_program()
    if hint and hint not in order:
        order.append(hint)
    # 3. dentro do tmux → janela no tmux atual
    if os.environ.get("TMUX") and "tmux" not in order:
        order.append("tmux")
    # 4. padrões da plataforma
    for name in _platform_defaults():
        if name not in order:
            order.append(name)

    launchers: list[Launcher] = []
    for name in order:
        builder = _BUILDERS.get(name)
        if builder is None:
            continue
        launcher = builder(cwd, cmd)
        if launcher is not None:
            launchers.append(launcher)
    return launchers


def build_open_argv(cwd: str, cmd: str, config: Config) -> tuple[list[str] | None, str]:
    """Monta o argv do terminal preferido. Retorna (argv, comando legível).

    argv None = nenhum terminal encontrado (chamador decide o que fazer).
    """
    launchers = build_launchers(cwd, cmd, config)
    if not launchers:
        return None, cmd
    return launchers[0].argv, launchers[0].display


# ------------------------------------------------------------- disparo


def _detach_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return kwargs


def _launch(argv: list[str]) -> None:
    """Popen destacado (fire-and-forget) — GUI terminals."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_detach_kwargs(),
    )
    log.info("abriu terminal: %s", argv)


def run_launcher(launcher: Launcher, timeout: float = CLI_TIMEOUT) -> bool:
    """Executa um launcher; True = abriu de verdade.

    "cli": subprocess.run com timeout — o rc diz se o terminal atendeu
    (kitty @ sem remote control, wezterm cli sem instância etc. falham
    aqui em vez de "abrir sem abrir nada"). "spawn": Popen destacado;
    OSError ao lançar devolve False (binário sumiu, args ruins).
    """
    if launcher.kind == "cli":
        try:
            proc = subprocess.run(
                launcher.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout,
                text=True,
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("launcher %s falhou: %s", launcher.label, exc)
            return False
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            log.warning(
                "launcher %s rc=%d: %s", launcher.label, proc.returncode, err
            )
            return False
        return True
    try:
        _launch(launcher.argv)
    except OSError as exc:
        log.warning("launcher %s falhou ao lançar: %s", launcher.label, exc)
        return False
    return True


def open_session(
    row: dict[str, Any],
    config: Config,
    cwd_override: str | None = None,
    dry_run: bool = False,
) -> OpenResult:
    """Abre a sessão `row` num terminal novo no worktree dela.

    TODO fase 3: se a sessão está rodando num pane do tmux, focar o pane
    em vez de abrir outro (exige o mapa de processos ↔ panes).
    """
    cwd = cwd_override or row.get("worktree_path") or row.get("cwd")
    cmd = row.get("resume_command") or ""
    if not cwd or not os.path.isdir(cwd):
        return OpenResult("missing", cmd, f"caminho não existe mais: {cwd}")
    if not cmd:
        return OpenResult("error", None, "sessão sem comando de retomada")

    if not dry_run:
        sid = str(row.get("session_id") or "")
        if not _SAFE_SESSION_ID_RE.fullmatch(sid):
            # id vem do nome do arquivo no disco; se não for um token
            # inofensivo, não embutimos em cmd /k, sh -c, tmux, AppleScript
            return OpenResult(
                "error",
                cmd,
                f"session_id suspeito para execução ({sid!r}) — copie o comando "
                "e rode manualmente",
            )

    launchers = build_launchers(cwd, cmd, config)
    if dry_run:
        if launchers:
            return OpenResult("dry_run", launchers[0].display, launchers[0].display)
        # sem terminal instalado: --dry-run não é erro, só mostra o comando
        return OpenResult("dry_run", cmd, cmd)

    failed: list[str] = []
    for launcher in launchers:
        if run_launcher(launcher):
            return OpenResult("opened", launcher.display, launcher.display)
        failed.append(launcher.label)

    if not failed:
        return OpenResult(
            "error",
            cmd,
            "nenhum terminal detectado — configure terminal ou open_command no config.toml",
        )
    return OpenResult(
        "error",
        cmd,
        f"nenhum terminal abriu (tentados: {', '.join(failed)}) — comando: {cmd}",
    )


def open_in_editor(path: str, config: Config) -> OpenResult:
    """Abre o worktree no editor ($EDITOR ou code)."""
    if not os.path.isdir(path):
        return OpenResult("missing", None, f"caminho não existe: {path}")
    editor = config.editor or "code"
    if shutil.which(editor) or os.path.isfile(editor):
        argv = [editor, path]
    else:
        argv = _split_template(editor) + [path]
    if not any(shutil.which(a) or os.path.isfile(a) for a in argv[:1]):
        return OpenResult("error", None, f"editor não encontrado: {editor}")
    try:
        _launch(argv)
    except OSError as exc:
        return OpenResult("error", " ".join(argv), f"falha ao lançar editor: {exc}")
    return OpenResult("opened", " ".join(argv), " ".join(argv))
