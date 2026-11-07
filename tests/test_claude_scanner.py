"""Testes do scanner do Claude Code (fixtures sintéticas)."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import (
    make_assistant_line,
    make_meta_line,
    make_summary_line,
    make_user_line,
    write_transcript,
)

from sessionhub.scanners.claude import ClaudeScanner

SID = "11111111-2222-3333-4444-555555555555"


def _write_custom_title(projects: Path, sid: str, content: str) -> None:
    """Grava custom-title.json na pasta irmã do transcript (layout do /rename)."""
    d = projects / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "custom-title.json").write_text(content, encoding="utf-8")


def test_parses_sessao_basica(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_meta_line(SID),
            make_user_line(
                SID,
                "G:\\proj",
                "<local-command-caveat>xyz</local-command-caveat>",
                ts="2026-11-01T10:00:00.000Z",
                meta=True,
            ),
            make_user_line(
                SID, "G:\\proj", "consertar o pipeline de build", ts="2026-11-01T10:00:01.000Z"
            ),
            make_assistant_line(SID, "G:\\proj", "ok, vou ver", ts="2026-11-01T10:01:00.000Z"),
            make_user_line(SID, "G:\\proj", "obrigado", ts="2026-11-01T10:02:00.000Z"),
        ],
    )
    scanner = ClaudeScanner(fake_home)
    result = scanner.discover()
    assert len(result.sessions) == 1
    s = result.sessions[0]
    assert s.harness == "claude"
    assert s.session_id == SID
    assert s.title == "consertar o pipeline de build"
    assert s.message_count == 3  # 2 user reais + 1 assistant (meta não conta)
    assert s.cwd == "G:\\proj"
    assert s.branch == "main"
    assert s.created_at is not None and s.created_at.year == 2026
    assert s.last_activity is not None and s.last_activity.minute == 2
    assert s.resume_command == f"claude --resume {SID}"
    assert s.transcript_path == str(projects / f"{SID}.jsonl")


def test_comando_local_nao_vira_titulo(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(
                SID,
                "G:\\proj",
                "<command-name>/clear</command-name>\n...",
                ts="2026-11-01T10:00:00.000Z",
            ),
            make_user_line(SID, "G:\\proj", "oi, tudo bem?", ts="2026-11-01T10:00:05.000Z"),
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title == "oi, tudo bem?"


def test_titulo_usa_summary_como_fallback(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_summary_line("Refatora módulo de scan"),
            make_meta_line(SID),
            {"type": "cost-state", "sessionId": SID, "totalCostUSD": 0},
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title == "Refatora módulo de scan"
    assert s.message_count == 0


def test_linhas_corrompidas_sao_puladas(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            "isto não é json {{{",
            json.dumps(make_user_line(SID, "G:\\proj", "msg boa")),
            "outra linha quebrada ]]]",
            json.dumps(make_assistant_line(SID, "G:\\proj", "ok", "2026-11-01T10:01:00.000Z")),
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.message_count == 2
    assert s.title == "msg boa"


def test_titulo_longue_e_truncado(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\proj", "a" * 200)],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title is not None and len(s.title) == 80


def test_transcript_sem_mensagens_reais(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_meta_line(SID),
            {"type": "cost-state", "sessionId": SID, "totalCostUSD": 0},
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title is None
    assert s.session_id == SID  # fallback: stem do arquivo
    assert s.last_activity is not None  # do mtime do arquivo


def test_gitbranch_head_e_ignorada(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\proj", "x", branch="HEAD")],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.branch is None


def test_incremental_pula_inalterados(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    fp = write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, "G:\\proj", "x")])
    scanner = ClaudeScanner(fake_home)
    first = scanner.discover()
    assert first.skipped == 0 and len(first.sessions) == 1

    stat = fp.stat()
    second = scanner.discover(fingerprints={str(fp): (stat.st_mtime, stat.st_size)})
    assert second.skipped == 1
    assert second.sessions == []
    assert str(fp) in second.seen


def test_diretorio_ausente(fake_home: Path) -> None:
    scanner = ClaudeScanner(fake_home / "nao-existe")
    assert scanner.discover().sessions == []


def test_cwd_usa_ultimo_valor(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(SID, "G:\\proj", "começa", ts="2026-11-01T10:00:00.000Z"),
            make_user_line(SID, "G:\\outro", "termina", ts="2026-11-01T11:00:00.000Z"),
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.cwd == "G:\\outro"


def test_transcript_de_subagente_e_ignorado(fake_home: Path) -> None:
    """Sidechains (subagents/) não são sessões — não entram no índice."""
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\proj", "sessão principal")],
    )
    write_transcript(
        projects / "subagents" / "agent-abc.jsonl",
        [make_user_line(SID, "G:\\proj", "sou um subagente")],  # sessionId = o da pai
    )
    result = ClaudeScanner(fake_home).discover()
    assert len(result.sessions) == 1
    assert result.sessions[0].title == "sessão principal"


def test_agent_jsonl_solto_na_pasta_e_ignorado(fake_home: Path) -> None:
    """Formato antigo: subagente como agent-*.jsonl direto na pasta do projeto
    (com sessionId da sessão pai) — não pode engolir a sessão principal."""
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\proj", "sessão principal", ts="2026-11-01T10:00:00Z")],
    )
    old_format = write_transcript(
        projects / "agent-99998888.jsonl",
        [
            # sessionId = o da pai; é por isso que o nome do arquivo decide
            make_user_line(SID, "G:\\proj", "subagente antigo", ts="2026-11-01T11:00:00Z")
        ],
    )
    result = ClaudeScanner(fake_home).discover()
    assert len(result.sessions) == 1
    assert result.sessions[0].session_id == SID
    assert result.sessions[0].title == "sessão principal"
    assert str(old_format) not in result.seen


def test_arquivo_todo_sidechain_nao_e_sessao(fake_home: Path) -> None:
    """Mesmo com outro nome, transcript só com linhas sidechain não é sessão."""
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, "G:\\proj", "principal")])
    write_transcript(
        projects / "77777777-6666-5555-4444-333333333333.jsonl",
        [make_user_line(SID, "G:\\proj", "sidechain", sidechain=True)],
    )
    result = ClaudeScanner(fake_home).discover()
    assert [s.session_id for s in result.sessions] == [SID]


def test_id_da_sessao_e_o_stem_do_arquivo(fake_home: Path) -> None:
    """O claude --resume procura pelo nome do arquivo — o sessionId interno
    (que em subagentes é o da pai) nunca define o id."""
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
        [make_user_line(SID, "G:\\proj", "mensagem")],  # sessionId diferente do stem
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert s.resume_command == "claude --resume aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ------------------------------------------------------------- diagnose (doctor)


def test_diagnose_encontra_transcripts(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, "G:\\proj", "oi")])
    diag = ClaudeScanner(fake_home).diagnose()
    assert diag.name == "claude"
    assert diag.found is True
    assert diag.files == 1
    assert diag.note is None
    assert diag.problem is False


def test_diagnose_nao_instalado(fake_home: Path) -> None:
    diag = ClaudeScanner(fake_home / "nao-existe").diagnose()
    assert diag.found is False
    assert diag.files == 0
    assert diag.note is not None
    assert diag.problem is False  # não instalado é nota, não problema


def test_diagnose_origem_vazia_e_problema(fake_home: Path) -> None:
    """Projects existe mas sem transcripts → problema (o doctor pinta vermelho)."""
    (fake_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    diag = ClaudeScanner(fake_home).diagnose()
    assert diag.found is True
    assert diag.files == 0
    assert diag.note is not None
    assert diag.problem is True


# ------------------------------------------------------------- /rename (título custom)


def test_rename_vence_primeira_mensagem(fake_home: Path) -> None:
    """custom-title.json do /rename tem prioridade sobre a 1ª mensagem."""
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, "G:\\proj", "primeira msg")])
    _write_custom_title(projects, SID, '{"customTitle":"meu nome"}')
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title == "meu nome"


def test_rename_em_arquivo_inalterado_vai_sem_reparse(fake_home: Path) -> None:
    """Scan incremental: jsonl não mudou → só o título novo é reportado."""
    projects = fake_home / ".claude" / "projects" / "G--proj"
    fp = write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, "G:\\proj", "msg")])
    stat = fp.stat()
    fingerprints = {str(fp): (stat.st_mtime, stat.st_size)}

    result = ClaudeScanner(fake_home).discover(fingerprints)
    assert result.sessions == []
    assert result.renames == {}

    # /rename chegou DEPOIS da indexação (transcript intacto)
    _write_custom_title(projects, SID, '{"customTitle":"renomeada"}')
    result = ClaudeScanner(fake_home).discover(fingerprints)
    assert result.sessions == []  # nada foi re-parseado
    assert result.renames == {str(fp): "renomeada"}


def test_rename_corrompido_ou_vazio_ignorado(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, "G:\\proj", "msg")])

    _write_custom_title(projects, SID, "não é json")
    assert ClaudeScanner(fake_home).discover().sessions[0].title == "msg"

    _write_custom_title(projects, SID, '{"customTitle":"   "}')
    assert ClaudeScanner(fake_home).discover().sessions[0].title == "msg"


def test_dedup_mesmo_stem_em_pastas_diferentes(fake_home: Path) -> None:
    """Mesmo stem em dois projetos: fica o mais recente (safety net)."""
    p1 = fake_home / ".claude" / "projects" / "G--um"
    p2 = fake_home / ".claude" / "projects" / "G--dois"
    write_transcript(
        p1 / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\um", "antigo", ts="2026-11-01T10:00:00.000Z")],
    )
    write_transcript(
        p2 / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\dois", "novo", ts="2026-11-02T10:00:00.000Z")],
    )
    result = ClaudeScanner(fake_home).discover()
    assert len(result.sessions) == 1
    assert result.sessions[0].title == "novo"


def test_titulo_desembrulha_pasted_content(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(
                SID,
                "G:\\proj",
                '<pasted_content id="2f8a">\n'
                "InitializeDriver: begin\nmap_fuse_driver: begin\n</pasted_content>",
                ts="2026-11-01T10:00:00.000Z",
            )
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title is not None
    assert s.title.startswith("InitializeDriver: begin")


def test_titulo_remove_system_reminder(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--proj"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(
                SID,
                "G:\\proj",
                "<system-reminder>contexto interno</system-reminder> analisa esse dump",
                ts="2026-11-01T10:00:00.000Z",
            )
        ],
    )
    s = ClaudeScanner(fake_home).discover().sessions[0]
    assert s.title is not None
    assert s.title.startswith("analisa esse dump")
