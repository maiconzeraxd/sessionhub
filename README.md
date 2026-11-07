# SessionHub

Encontra, monitora e retoma sessões de agentes de código (Claude Code, e mais
harnesses nas próximas fases) — com os worktrees de git de cada uma — num
lugar só: uma TUI no terminal.

```
📁 North (G:\…) · 7 worktrees
  🌿 main · ✎12 ↑3 · PR#42
    🟡 conserta o pipeline de build · claude · 5min · 8m
    ⚪ refatora o scanner · claude · 2d · 21m 📌
  🌂 feat/imgui · sem sessões
📂 (sem repositório) · 3 pastas
```

Fases (a do momento marcada):
1. ✅ Scanner Claude Code + worktrees git + índice SQLite + `sessionhub ls`
2. ✅ TUI (árvore, filtros, busca, preview) + abrir sessão no terminal
3. ⬜ Monitoramento ao vivo (hooks + psutil + watcher): 🟢 rodando / 🟡 esperando você
4. ⬜ Backup incremental + restore + agendamento + age
5. ⬜ Scanners Codex (`.jsonl.zst`), Gemini CLI, Aider + busca full-text FTS5
6. ⬜ Daemon + notificações + doctor polish

## Instalação

Requisitos: Python 3.11+, git no PATH. (uv recomendado; o venv não precisa de pip.)

```powershell
cd G:\_IA\Aplicação
uv venv
uv pip install -e ".[dev]" --python .venv\Scripts\python.exe
.venv\Scripts\sessionhub.exe --help
```

Ou com pip/pipx em qualquer máquina: `pipx install .`.

> **Gotcha (uv + pasta com acento):** em caminhos com caracteres não-ASCII
> (este projeto: `G:\_IA\Aplicação`), o editable install do uv grava o `.pth`
> com encoding errado e o `import sessionhub` falha fora do diretório do
> projeto. Correção de 1 linha — reescreva
> `.venv\Lib\site-packages\_editable_impl_sessionhub.pth` com:
>
> ```text
> import sys, os; _p = os.path.dirname(sys.prefix); sys.path.insert(0, _p) if (_p not in sys.path and os.path.isdir(os.path.join(_p, "sessionhub"))) else None
> ```
>
> (ASCII puro, deriva o caminho do `sys.prefix` — imune ao encoding.) Uma
> instalação normal (`pipx install .`) não tem esse problema.

## Uso

```powershell
sessionhub            # abre a TUI (ou `python -m sessionhub`)
sessionhub scan       # atualiza o índice (incremental; --full = reparse tudo)
sessionhub ls         # tabela no terminal
sessionhub ls --status forgotten --limit 20
sessionhub ls --harness claude --json
sessionhub show 3aee5ee9            # o que foi digitado: pedido inicial + últimas mensagens
sessionhub show 3aee5ee9 -n 30      # mais mensagens (-n 0 = só o pedido inicial)
sessionhub open 3aee5ee9            # prefixo do session_id
sessionhub open 3aee --dry-run      # só mostra o comando (nada executa)
sessionhub open 3aee --copy         # copia o comando de retomada
sessionhub doctor                   # diagnóstico de harnesses/ferramentas/terminal
```

### TUI — atalhos

| tecla | ação |
|---|---|
| `↑ ↓ → ←` | navegar (setas expandem worktrees) |
| **clique** | selecionar sessão — painel direito mostra o que foi digitado |
| **duplo-clique** | abrir a sessão num terminal novo, no worktree dela |
| `Enter` | abrir a sessão (igual ao duplo-clique) |
| `1 2 3 4` | filtros: Ativas · Esperando você · Esquecidas · Todas |
| `/` | busca fuzzy (título, branch, caminho, session_id) |
| `Tab` | alternar foco árvore ↔ busca |
| `n` | adicionar/editar nota 📌 e tags da sessão |
| `c` | copiar o comando de retomada |
| `o` | abrir o worktree no editor (`code .`) |
| `b` | alternar branch/estado git no preview |
| `x` | arquivar/ocultar sessão (reversível com `H`) |
| `H` | mostrar/ocultar arquivadas |
| `s` | re-escanear (dentro da TUI) |
| `r` | refresh |
| `q` | sair |
| `?` | ajuda |

O painel direito mostra o **pedido inicial** (primeira mensagem real do
usuário, com invólucros de paste/reminder desembrulhados), as **últimas
mensagens** da conversa (👤/🤖, com timestamps), estado do git
(dirty/ahead/behind/PR), comando de retomada e a nota — rola com o mouse.
Sem daemon: refresh automático a cada 5s comparando uma assinatura do índice.

### Abertura de sessão

A ordem de resolução do terminal é: `open_command` do config.toml → tmux (se
dentro de `$TMUX`) → Windows Terminal (`wt.exe -d <worktree>`) → emadores
POSIX conhecidos. No Windows Terminal daqui, o comando gerado é:

```
wt.exe -d G:\Dev\North cmd /k claude --resume <id>
```

Personalize no `config.toml`:

```toml
open_command = 'meu-terminal --cwd {cwd} --exec {cmd}'
```

## Configuração (`~/.sessionhub/config.toml`)

Criado com comentários no primeiro uso. Campos principais:

| campo | padrão | descrição |
|---|---|---|
| `forgotten_hours` | `24` | worktree sujo + sem atividade além disso = 🔴 esquecido |
| `terminal` | `auto` | `wt` / `tmux` / `auto` |
| `open_command` | — | template com `{cwd}` e `{cmd}` |
| `editor` | `code` | usado pela tecla `o` |
| `extra_repos` | `[]` | repos sem sessão Claude (ex.: só Codex) |

App data vive só em `~/.sessionhub/` (`config.toml`, `index.db`, `logs/`).
Override para testes: variável `SESSIONHUB_HOME`.

## Arquitetura

```
sessionhub/
  cli.py          Typer: scan/ls/open/doctor + invoca a TUI sem subcomando
  tui.py          Textual: árvore repo→worktree→sessão, preview, modais
  scan.py         orquestração: scanners → git → índice (uma passada)
  scanners/       protocolo Scanner (name + discover) + claude.py
  db.py           SQLite (WAL): sessions, worktrees, events, meta
  gitinfo.py      worktree list/status/PRs (chamadas paralelas)
  preview.py      tail do transcript → markdown
  open.py         monta+dispara o comando do terminal
  config.py       config.toml + $SESSIONHUB_HOME
```

**Scanners são plugins.** Um harness novo entra sem mexer no núcleo:

```python
# meu_scanner.py — instalável à parte com entry point group "sessionhub.scanners"
from sessionhub.models import DiscoverResult, Session

class CodexScanner:
    name = "codex"
    def discover(self, fingerprints=None) -> DiscoverResult: ...
```

```toml
[project.entry-points."sessionhub.scanners"]
codex = "meu_scanner:CodexScanner"
```

Builtin (fase 1): `claude`. Fase 5: `codex` (.jsonl.zst), `gemini`, `aider`,
`generic` (qualquer pasta de `.jsonl`).

### Detalhes que importam

- **Somente leitura nos harnesses**: o SessionHub nunca escreve/move/apaga
  transcripts. Escrita só em `~/.sessionhub/` (hooks e backup, quando
  existirem, são as únicas exceções confirmadas).
- **Incremental**: cada transcript tem fingerprint (mtime+size) no índice;
  inalterado não é relido. 366 transcripts / 2,4GB: scan completo ~10s,
  warm ~0,9s.
- **Pre-filtro de linhas**: só linhas com marcador `"type":"user"/"assistant"/"summary"`
  passam por `json.loads` (perf em transcripts de GB).
- **Subagentes não são sessões**: `projects/<p>/subagents/*.jsonl` é pulado
  (o sessionId interno é o da sessão pai).
- **Branch vem do git, não do transcript**: `gitBranch:"HEAD"` é unreliable;
  o branch real vem do `git status` do worktree.
- **Títulos**: primeira mensagem real do usuário, pulando comandos/meta/
  task-notifications, desembrulhando `<pasted_content>`/`<system-reminder>`;
  fallback para o `summary` (escrito pelo Claude em compacts).

## Desenvolvimento

```powershell
uv pip install -e ".[dev]" --python .venv\Scripts\python.exe
python -m pytest      # 80 testes (fixtures sintéticas em tmp; nada real)
python -m ruff check .
python -m mypy
```

Testes nunca tocam dados reais: `SESSIONHUB_HOME` + home falso via
monkeypatch; transcripts são gerados em `tmp_path`. A suíte inclui o alvo
de performance (2.000 transcripts, scan warm < 1s).
