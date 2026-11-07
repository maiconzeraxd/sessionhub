# SessionHub

Encontra, monitora e retoma sessões de agentes de código (Claude Code, e mais
harnesses nas próximas fases) — com os worktrees de git de cada uma — num
lugar só: uma TUI no terminal.

```
📁 North (~/dev/north) · 7 worktrees
  🌿 main · ✎12 ↑3 · PR#42
    🟡 conserta o pipeline de build · claude · 5min · 8m
    ⚪ refatora o scanner · claude · 2d · 21m 📌
  🔴 feat/imgui · esquecida · 3d        ← trabalho não entregue, sem sessão
  🌿 feat/json · sem sessões · 2d
  🗄 migra o banco · claude · 5d         ← transcript sumiu da origem
📂 (sem repositório) · 3 pastas
```

Fases (a do momento marcada):
1. ✅ Scanner Claude Code + worktrees git + índice SQLite + `sessionhub ls`
2. ✅ TUI (árvore, filtros, busca, preview) + abrir sessão no terminal
3. ⬜ Monitoramento ao vivo (hooks + psutil): 🟢 rodando / 🟡 esperando você
   (o watcher de transcripts — scan quente na TUI — já está ativo)
4. 🟡 Backup (primeiro corte pronto: `b` → pasta datada, um `.md` por
   sessão) + restore + agendamento + age
5. ⬜ Scanners Codex (`.jsonl.zst`), Gemini CLI, Aider + busca full-text FTS5
6. ⬜ Daemon + notificações + doctor polish

## Instalação

Plataformas: **Windows**, **Linux** e **macOS**. Requisitos: Python 3.11+ e
git no PATH (worktrees e PRs). Nada mais — o terminal é detectado na hora
(Windows Terminal, kitty, wezterm, tmux, iTerm, gnome-terminal, …).

Em qualquer máquina:

```bash
git clone https://github.com/maiconzeraxd/sessionhub.git
cd sessionhub
```

Com [uv](https://docs.astral.sh/uv/) (recomendado — o venv não precisa de pip):

```bash
uv venv
uv pip install -e . --python .venv/bin/python          # Linux/macOS
uv pip install -e . --python .venv/Scripts/python.exe  # Windows
```

Ou com o pip do Python:

```bash
python -m venv .venv
.venv/bin/pip install -e .        # Linux/macOS
.venv\Scripts\pip install -e .    # Windows
```

O executável fica no venv (`sessionhub`; `python -m sessionhub` também
funciona). Instalação global isolada:
`pipx install git+https://github.com/maiconzeraxd/sessionhub.git`.

> **Gotcha (uv + pasta com acento):** se o caminho do projeto contém
> caracteres não-ASCII (acentos, "ç", CJK…), o editable install do uv grava
> o `.pth` com encoding errado e o `import sessionhub` falha fora do
> diretório do projeto. Correção de 1 linha — reescreva
> `.venv/Lib/site-packages/_editable_impl_sessionhub.pth`
> (Linux/macOS: `.venv/lib/python3.11/site-packages/…`) com:
>
> ```text
> import sys, os; _p = os.path.dirname(sys.prefix); sys.path.insert(0, _p) if (_p not in sys.path and os.path.isdir(os.path.join(_p, "sessionhub"))) else None
> ```
>
> (ASCII puro, deriva o caminho do `sys.prefix` — imune ao encoding.) Uma
> instalação com pip/pipx não tem esse problema.

## Uso

```text
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
| `/` | busca fuzzy — título, branch, session_id, nota… (aceita sem acento); `Enter` volta pra árvore, `Esc` limpa |
| `Tab` | alternar foco árvore ↔ busca |
| `n` | adicionar/editar nota 📌 e tags da sessão |
| `m` | marcar/desmarcar sessão para o backup (✓ no rótulo) |
| `c` | copiar o comando de retomada |
| `o` | abrir o worktree no editor (`code .`) |
| `b` | backup das sessões marcadas — ou da selecionada — em pasta datada |
| `x` | arquivar/ocultar sessão (reversível com `H`) |
| `H` | mostrar/ocultar arquivadas |
| `s` | re-escanear (dentro da TUI) |
| `r` | refresh |
| `q` | sair |
| `?` | ajuda |

O painel direito abre nas **últimas mensagens** da conversa (👤/🤖, com
timestamps) — o que rolou na última vez que você usou a sessão —, com a
**data do último acesso**, o **tamanho do transcript** e o nº de mensagens
no cabeçalho (o tamanho também aparece no rótulo de cada sessão da
árvore), estado do git (dirty/ahead/behind/PR), comando de retomada e a
nota; o **pedido inicial** (primeira mensagem real, desembrulhada) fica no
fim. Rola com o mouse. A leitura do transcript roda fora do event loop
(transcripts de GB não travam a TUI) com debounce e cache por
(caminho, mtime).

### Backup ( `b` )

Marque sessões com `m` (✓ no rótulo) e aperte `b` — sem marcas, vale a
sessão selecionada. Um modal confirma a lista e o destino (campo editável,
pré-preenchido com o `[backup] dir` do config). Confirmar cria

```
<destino>/2026-11-07_15-30/
  Conserta o scanner__3aee5ee9.md
  Migra o banco__9f39ecf6.md
```

— pasta com a data, **um `.md` por sessão** (título sanitizado + prefixo
do id no nome). Cada `.md` traz um cabeçalho com os metadados (id, datas,
branch, worktree, tamanho, nota, comando de retomada) e **a conversa
completa** em Markdown, transcrita em streaming (transcripts de GB não
estouram a memória). Transcript que sumiu vira erro visível, não backup
silencioso. A escrita acontece **só no destino** — os transcripts dos
harnesses são apenas lidos.

Refresh: um **watcher** (watchdog) observa `~/.claude/projects` e dispara um
scan quente quando transcripts mudam (~2s); sem watchdog instalado — ou fora
dessas pastas — vale o refresh de 5s comparando a assinatura do índice.
Rebuilds preservam worktrees colapsadas e a rolagem; a busca tem debounce.

### Abertura de sessão

Ordem de resolução do terminal:

1. `open_command` do config.toml — template dividido em argumentos **antes**
   da substituição (`{cwd}`/`{cmd}`), então caminhos com espaço não quebram;
2. `terminal` explícito do config (`kitty`, `wezterm`, `tmux`, `wt`,
   `ghostty`, `iterm`, `apple-terminal`, `alacritty`, `gnome-terminal`, …);
3. o terminal em que o SessionHub roda (`$KITTY_WINDOW_ID`, `$WEZTERM_PANE`,
   `$TMUX`, `$TERM_PROGRAM`) — abre aba/pane/janela nova na mesma sessão;
4. padrão da plataforma (Windows: `wt`; macOS: kitty/ghostty/wezterm/iTerm/
   Terminal.app; Linux: kitty/wezterm/alacritty/gnome-terminal/konsole/
   x-terminal-emulator).

Launchers "cli" (kitty `@`, wezterm `cli`, tmux, osascript no macOS) são
**verificados** — o SessionHub checa o retorno e, se falhou, tenta o próximo
da lista; abrir nunca falha em silêncio. No Windows Terminal, o comando
gerado é:

```
wt.exe -d <caminho-do-worktree> cmd /k claude --resume <id>
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
| `terminal` | `auto` | forçar um terminal: `wt`/`kitty`/`wezterm`/`tmux`/… |
| `open_command` | — | template com `{cwd}` e `{cmd}` (tem prioridade sobre tudo) |
| `editor` | `code` | usado pela tecla `o` |
| `extra_repos` | `[]` | repos sem sessão Claude (ex.: só Codex) |
| `backup.dir` | `~/.sessionhub/backups` | destino dos backups da tecla `b` (editável no modal) |
| `retention.days` | `30` | eventos (hooks, fase 3) mais velhos que isso saem a cada scan |

App data vive só em `~/.sessionhub/` (`config.toml`, `index.db`, `logs/`).
Override para testes: variável `SESSIONHUB_HOME`.

## Arquitetura

```
sessionhub/
  cli.py          Typer: scan/ls/show/open/doctor + invoca a TUI sem subcomando
  tui.py          Textual: árvore repo→worktree→sessão, preview, modais
  scan.py         orquestração: scanners → git → índice (uma passada)
  scanners/       protocolo Scanner (name + discover + diagnose) + claude.py
  db.py           SQLite (WAL): sessions, worktrees, events, meta (+migrações)
  gitinfo.py      worktree list/status/PRs (paralelo; PRs com cache de 5min)
  preview.py      tail do transcript → markdown
  backup.py       sessões → .md em pasta datada (streaming, conversa completa)
  open.py         resolução+verificação de terminal, montagem do comando
  terminals.py    detecção do terminal atual (env) p/ abertura de sessão
  watcher.py      watchdog → scan quente na TUI quando transcripts mudam
  fmt.py          formatadores compartilhados (idade, badges git, caminhos)
  config.py       config.toml + $SESSIONHUB_HOME
```

**Scanners são plugins.** Um harness novo entra sem mexer no núcleo:

```python
# meu_scanner.py — instalável à parte com entry point group "sessionhub.scanners"
from sessionhub.models import DiscoverResult, HarnessDiagnosis, Session

class CodexScanner:
    name = "codex"
    def discover(self, fingerprints=None) -> DiscoverResult: ...
    def diagnose(self) -> HarnessDiagnosis: ...   # opcional p/ plugins
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
  (o sessionId interno é o da sessão pai); id da sessão = nome do arquivo
  (é o que o `claude --resume` procura).
- **🗄 Missing**: transcript sumiu da origem → status `missing` (a sessão
  fica no índice para retomar via repo principal; volta a `stopped` se o
  arquivo reaparecer).
- **🔴 Worktrees esquecidas**: trabalho não entregue (dirty/ahead) + sem
  sessão viva aparecem na árvore mesmo sem sessão indexada.
- **Branch vem do git, não do transcript**: `gitBranch:"HEAD"` é unreliable;
  o branch real vem do `git status` do worktree.
- **Títulos**: `/rename` vence (o `custom-title.json` gravado pelo Claude
  Code); senão a primeira mensagem real do usuário, pulando comandos/meta/
  task-notifications, desembrulhando `<pasted_content>`/`<system-reminder>`;
  fallback para o `summary` (escrito pelo Claude em compacts). Um `/rename`
  depois da indexação entra no índice sem re-parsear o transcript.
- **PRs com cache**: `gh pr list` roda com TTL de 5min por repo (rede é
  lenta; `scan --full` zera o cache).
- **Migrações**: o schema evolui via `PRAGMA user_version` — um `index.db`
  antigo é migrado na abertura, sem perder dados.

## Desenvolvimento

```bash
uv pip install -e ".[dev]" --python .venv/Scripts/python.exe  # Windows
uv pip install -e ".[dev]" --python .venv/bin/python          # Linux/macOS
python -m pytest      # 159 testes (fixtures sintéticas em tmp; nada real)
python -m ruff check .
python -m mypy
```

Testes nunca tocam dados reais: `SESSIONHUB_HOME` + home falso via
monkeypatch; transcripts são gerados em `tmp_path`. A suíte inclui o alvo
de performance (2.000 transcripts, scan warm < 1s).
