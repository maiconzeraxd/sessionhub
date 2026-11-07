"""Testes da pré-visualização (tail do transcript)."""

from __future__ import annotations

from pathlib import Path

from helpers import (
    make_assistant_line,
    make_meta_line,
    make_user_line,
    write_transcript,
)

from sessionhub.preview import first_user_message, preview_markdown, tail_messages

SID = "11111111-2222-3333-4444-555555555555"


def test_tail_ultimas_mensagens(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--p"
    fp = write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_meta_line(SID),
            make_user_line(SID, "G:\\p", "primeira pergunta", ts="2026-11-01T10:00:00Z"),
            make_assistant_line(SID, "G:\\p", "primeira resposta", "2026-11-01T10:00:30Z"),
            make_user_line(SID, "G:\\p", "segunda pergunta", ts="2026-11-01T10:01:00Z"),
            make_assistant_line(SID, "G:\\p", "segunda resposta", "2026-11-01T10:01:30Z"),
        ],
    )
    msgs = tail_messages(str(fp), "claude", max_messages=3)
    assert len(msgs) == 3  # corta as mais antigas
    assert msgs[0].text == "primeira resposta"
    assert msgs[-1].text == "segunda resposta"
    assert msgs[-1].role == "assistant"
    assert msgs[0].ts == "01/11 10:00"


def test_tail_pula_meta_e_sidechain(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--p"
    fp = write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(SID, "G:\\p", "<local-command-caveat>x", meta=True),
            make_user_line(SID, "G:\\p", "subagente", sidechain=True),
            make_user_line(SID, "G:\\p", "mensagem real"),
        ],
    )
    msgs = tail_messages(str(fp), "claude")
    assert [m.text for m in msgs] == ["mensagem real"]


def test_tail_arquivo_grande_le_so_final(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--p"
    fp = write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(SID, "G:\\p", "antiga " + "x" * 400_000),
            make_user_line(SID, "G:\\p", "recente", ts="2026-11-01T12:00:00Z"),
        ],
    )
    msgs = tail_messages(str(fp), "claude", max_bytes=100_000)
    # a linha parcial/gigante do começo é descartada; só a recente sobrevive
    assert [m.text for m in msgs] == ["recente"]


def test_tail_arquivo_inexistente() -> None:
    assert tail_messages("Z:\\nao\\existe.jsonl", "claude") == []
    assert tail_messages(None, "claude") == []
    assert tail_messages("qualquer", "codex") == []  # fase 5


def test_preview_markdown(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--p"
    lines = [
        make_meta_line(SID),
        make_user_line(SID, "G:\\p", "pergunta de teste", ts="2026-11-01T10:00:00Z"),
    ]
    for i in range(2, 40):
        ts = f"2026-11-01T10:{i:02d}:00Z"
        role_user = i % 2 == 0
        lines.append(
            make_user_line(SID, "G:\\p", f"pergunta {i}", ts=ts)
            if role_user
            else make_assistant_line(SID, "G:\\p", f"resposta {i}", ts)
        )
    fp = write_transcript(projects / f"{SID}.jsonl", lines)
    row = {
        "harness": "claude",
        "session_id": SID,
        "title": "Sessão de teste",
        "status": "stopped",
        "branch": "main",
        "worktree_path": "G:\\p",
        "cwd": "G:\\p",
        "created_at": "2026-11-01T10:00:00Z",
        "last_activity": "2026-11-01T10:05:00Z",
        "message_count": len(lines),
        "transcript_path": str(fp),
        "resume_command": f"claude --resume {SID}",
        "note": "",
        "tags": "",
    }
    md = preview_markdown(row, worktree={"staged": 1, "unstaged": 2, "untracked": 0, "ahead": 1})
    assert "Sessão de teste" in md
    assert "claude --resume" in md
    assert "✎ 3" in md and "↑1" in md
    assert "👤 você" in md
    assert "**tamanho:**" in md  # tamanho do transcript no cabeçalho
    assert "**último acesso:**" in md  # data absoluta + idade (não só "5min")
    # ordem: últimas mensagens primeiro, pedido inicial no fim
    assert md.index("Últimas mensagens") < md.index("Pedido inicial")
    assert "pergunta de teste" in md
    assert "> pergunta 2\n" not in md  # caíram fora das últimas 25


def test_preview_markdown_sessao_de_1_msg_sem_duplicar(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--p"
    fp = write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\p", "única mensagem")],
    )
    row = {
        "harness": "claude",
        "session_id": SID,
        "title": "única",
        "status": "stopped",
        "transcript_path": str(fp),
        "resume_command": "claude --resume x",
        "message_count": 1,
    }
    md = preview_markdown(row)
    # pedido inicial == única mensagem do tail → não repete
    assert md.count("única mensagem") == 1
    assert "Pedido inicial" not in md


def test_first_user_message_pula_ruido(fake_home: Path) -> None:
    projects = fake_home / ".claude" / "projects" / "G--p"
    fp = write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_meta_line(SID),
            make_user_line(SID, "G:\\p", "<command-name>/clear</command-name>", meta=True),
            make_user_line(
                SID,
                "G:\\p",
                "<pasted_content id=\"1\">\ncoloquei um log aqui\n</pasted_content>",
                ts="2026-11-01T10:00:00Z",
            ),
        ],
    )
    msg = first_user_message(str(fp), "claude")
    assert msg is not None
    assert msg.text == "coloquei um log aqui"
    assert msg.ts == "01/11 10:00"


def test_first_user_message_none(fake_home: Path) -> None:
    assert first_user_message(None, "claude") is None
    assert first_user_message("Z:\\nao\\existe.jsonl", "claude") is None
    assert first_user_message("qualquer", "codex") is None
    projects = fake_home / ".claude" / "projects" / "G--p"
    fp = write_transcript(
        projects / f"{SID}.jsonl",
        [make_meta_line(SID), make_user_line(SID, "G:\\p", "sub", sidechain=True)],
    )
    assert first_user_message(str(fp), "claude") is None
