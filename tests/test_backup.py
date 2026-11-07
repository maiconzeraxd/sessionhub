"""Testes do backup de sessões (tudo sintético, em tmp_path)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from helpers import make_assistant_line, make_user_line, write_transcript

from sessionhub.backup import backup_sessions

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "99999999-8888-7777-6666-555555555555"
NOW = datetime(2026, 11, 7, 15, 30, tzinfo=UTC)


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        harness="claude",
        session_id=SID,
        title="Conserta o scanner",
        cwd="C:/p",
        repo_root=None,
        worktree_path=None,
        branch=None,
        last_activity="2026-11-05T10:00:00Z",
        created_at="2026-11-01T10:00:00Z",
        message_count=2,
        transcript_path="",
        resume_command=f"claude --resume {SID}",
        status="stopped",
        note="",
        tags="",
    )
    base.update(kw)
    return base


def _transcript(tmp_path: Path, sid: str, lines: list[dict[str, object]]) -> str:
    projects = tmp_path / ".claude" / "projects" / "C--p"
    fp = write_transcript(projects / f"{sid}.jsonl", lines)  # type: ignore[arg-type]
    return str(fp)


def test_backup_cria_pasta_com_data_e_um_md_por_sessao(tmp_path: Path) -> None:
    tp = _transcript(
        tmp_path,
        SID,
        [
            make_user_line(SID, "C:/p", "primeira pergunta", ts="2026-11-01T10:00:00Z"),
            make_assistant_line(SID, "C:/p", "primeira resposta", "2026-11-01T10:00:30Z"),
        ],
    )
    result = backup_sessions([_row(transcript_path=tp)], tmp_path / "bk", now=NOW)

    # pasta com a data dentro do destino (que nem existia — é criada)
    assert result.folder == tmp_path / "bk" / "2026-11-07_15-30"
    assert result.folder.is_dir()
    # um .md com o nome da sessão + prefixo do id
    assert [f.name for f in result.files] == ["Conserta o scanner__11111111.md"]
    assert result.errors == []
    assert [r["session_id"] for r in result.done] == [SID]

    md = result.files[0].read_text(encoding="utf-8")
    assert md.startswith("# Conserta o scanner")
    assert "**retomada:**" in md
    assert f"`{SID}`" in md
    assert "👤 usuário" in md and "🤖 assistente" in md
    assert "> primeira pergunta" in md
    assert "> primeira resposta" in md
    assert "**tamanho:**" in md  # o "etc" do cabeçalho


def test_backup_duas_sessoes_dois_mds(tmp_path: Path) -> None:
    tp1 = _transcript(
        tmp_path, SID, [make_user_line(SID, "C:/p", "tarefa um")]
    )
    tp2 = _transcript(
        tmp_path, SID2, [make_user_line(SID2, "C:/p", "tarefa dois")]
    )
    result = backup_sessions(
        [
            _row(transcript_path=tp1),
            _row(session_id=SID2, title="Migra o banco", transcript_path=tp2),
        ],
        tmp_path / "bk",
        now=NOW,
    )
    assert len(result.files) == 2
    assert {f.name for f in result.files} == {
        "Conserta o scanner__11111111.md",
        "Migra o banco__99999999.md",
    }


def test_backup_sanitiza_titulo_no_nome_do_arquivo(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, SID, [make_user_line(SID, "C:/p", "x")])
    row = _row(
        title='Trabalho "final": parte/2 <rascunho>? | sim',
        transcript_path=tp,
    )
    result = backup_sessions([row], tmp_path / "bk", now=NOW)
    name = result.files[0].name
    for ch in '<>:"/\\|?*':
        assert ch not in name
    assert name.startswith("Trabalho ")


def test_backup_pasta_nao_colidi_no_mesmo_minuto(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, SID, [make_user_line(SID, "C:/p", "x")])
    r1 = backup_sessions([_row(transcript_path=tp)], tmp_path / "bk", now=NOW)
    r2 = backup_sessions([_row(transcript_path=tp)], tmp_path / "bk", now=NOW)
    assert r1.folder == tmp_path / "bk" / "2026-11-07_15-30"
    assert r2.folder == tmp_path / "bk" / "2026-11-07_15-30-2"


def test_backup_transcript_sumido_vira_erro_sem_quebrar(tmp_path: Path) -> None:
    row = _row(transcript_path=str(tmp_path / "nao-existe.jsonl"))
    result = backup_sessions([row], tmp_path / "bk", now=NOW)
    assert result.files == []
    assert result.done == []
    assert len(result.errors) == 1
    assert "Conserta o scanner" in result.errors[0]


def test_backup_transcreve_conversa_completa_nao_so_o_tail(tmp_path: Path) -> None:
    """O preview lê só as últimas 25 — o backup tem que ser a conversa toda."""
    lines = []
    for i in range(40):
        lines.append(make_user_line(SID, "C:/p", f"pergunta {i}"))
    tp = _transcript(tmp_path, SID, lines)

    result = backup_sessions([_row(transcript_path=tp)], tmp_path / "bk", now=NOW)
    md = result.files[0].read_text(encoding="utf-8")
    assert md.count("**👤 usuário**") == 40
    assert "> pergunta 0" in md and "> pergunta 39" in md


def test_backup_transcript_vazio_avisado_no_md(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, SID, [])
    result = backup_sessions([_row(transcript_path=tp)], tmp_path / "bk", now=NOW)
    md = result.files[0].read_text(encoding="utf-8")
    assert "transcript vazio" in md


def test_backup_titulos_iguais_nao_colidem(tmp_path: Path) -> None:
    tp1 = _transcript(tmp_path, SID, [make_user_line(SID, "C:/p", "a")])
    tp2 = _transcript(tmp_path, SID2, [make_user_line(SID2, "C:/p", "b")])
    result = backup_sessions(
        [
            _row(transcript_path=tp1),
            _row(session_id=SID2, transcript_path=tp2),
        ],
        tmp_path / "bk",
        now=NOW,
    )
    assert len(result.files) == 2
    assert len({f.name for f in result.files}) == 2  # sid desempata
