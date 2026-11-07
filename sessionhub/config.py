"""Configuração do SessionHub (~/.sessionhub/config.toml).

Criado automaticamente na primeira execução. Todo dado do app fica em
`~/.sessionhub/` (ou em $SESSIONHUB_HOME, usado pelos testes).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR_NAME = ".sessionhub"

DEFAULT_CONFIG_TOML = """\
# SessionHub — configuração
# Criado automaticamente na primeira execução.

# Horas sem atividade para um worktree sujo contar como "esquecido".
forgotten_hours = 24

# Terminal usado para abrir sessões:
#   "auto" detecta (tmux dentro do tmux; wt.exe no Windows; kitty/wezterm/...)
terminal = "auto"

# Template customizado (substitui tudo). {cwd} e {cmd} são expandidos.
# open_command = 'wt.exe -d {cwd} cmd /k {cmd}'

# Editor da tecla "o" (padrão: $EDITOR ou "code").
# editor = "code"

# Repos extras para monitorar worktrees, além dos descobertos pelas sessões.
extra_repos = []

# Raízes extras de busca (futuros harnesses genéricos).
scan_roots = []

[backup]
# Destino dos backups dos transcripts.
dir = "~/.sessionhub/backups"

[retention]
# Manter todos os snapshots dos últimos N dias; depois, 1 por semana.
days = 30
weekly = true

# Harnesses customizados (scanner genérico, glob + regex no nome do arquivo).
# [generic_scanners.meu-agente]
# root = "~/.meu-agente"
# glob = "**/*.jsonl"
# session_id = "[0-9a-f-]{36}"
# resume = "meu-agente resume {id}"
"""


@dataclass
class Config:
    """Configuração carregada (com defaults)."""

    home: Path
    db_path: Path
    log_dir: Path
    backup_dir: Path
    forgotten_hours: float = 24.0
    terminal: str = "auto"
    open_command: str | None = None
    editor: str | None = None
    extra_repos: list[str] = field(default_factory=list)
    scan_roots: list[str] = field(default_factory=list)
    retention_days: int = 30
    retention_weekly: bool = True
    generic: dict[str, dict[str, str]] = field(default_factory=dict)
    config_path: Path | None = None


def app_home() -> Path:
    """Diretório de dados do app ($SESSIONHUB_HOME ou ~/.sessionhub)."""
    env = os.environ.get("SESSIONHUB_HOME")
    if env:
        return Path(env).expanduser().absolute()
    return Path.home() / APP_DIR_NAME


def load_config(path: Path | None = None) -> Config:
    """Carrega config.toml (cria com defaults se não existir)."""
    home = app_home()
    cfg_path = path if path is not None else home / "config.toml"
    data: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)
    else:
        home.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    backup = data.get("backup", {}) if isinstance(data.get("backup"), dict) else {}
    retention = data.get("retention", {}) if isinstance(data.get("retention"), dict) else {}
    backup_dir = Path(str(backup.get("dir", "~/.sessionhub/backups"))).expanduser()
    if not backup_dir.is_absolute():
        backup_dir = home / backup_dir
    generic = data.get("generic_scanners", {})
    if not isinstance(generic, dict):
        generic = {}

    return Config(
        home=home,
        db_path=home / "index.db",
        log_dir=home / "logs",
        backup_dir=backup_dir,
        forgotten_hours=float(data.get("forgotten_hours", 24)),
        terminal=str(data.get("terminal", "auto")),
        open_command=data.get("open_command") or None,
        editor=data.get("editor") or os.environ.get("EDITOR"),
        extra_repos=[str(x) for x in data.get("extra_repos", [])],
        scan_roots=[str(x) for x in data.get("scan_roots", [])],
        retention_days=int(retention.get("days", 30)),
        retention_weekly=bool(retention.get("weekly", True)),
        generic={str(k): dict(v) for k, v in generic.items() if isinstance(v, dict)},
        config_path=cfg_path,
    )
