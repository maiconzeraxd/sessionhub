"""Exclusão de sessão: apaga SÓ os arquivos do próprio id, nada além."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sessionhub.delete import delete_session_files, session_paths

NOW = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
SID = "11111111-2222-3333-4444-555555555555"
OUTRA = "99999999-8888-7777-6666-555555555555"


def _proj(home: Path) -> Path:
    proj = home / ".claude" / "projects" / "G--dev-north"
    proj.mkdir(parents=True, exist_ok=True)
    return proj


def _row(tp: str | None, sid: str = SID) -> dict[str, object]:
    return {
        "harness": "claude",
        "session_id": sid,
        "title": "trabalho final",
        "transcript_path": tp or "",
        "cwd": "G:\\Dev\\North",
    }


def test_delete_apaga_transcript_e_pasta_do_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = _proj(home)
    tp = proj / f"{SID}.jsonl"
    tp.write_text("{}\n", encoding="utf-8")
    folder = proj / SID
    folder.mkdir()
    (folder / "custom-title.json").write_text("{}", encoding="utf-8")

    result = delete_session_files(_row(str(tp)), home, backup_first=False)

    assert sorted(p.name for p in result.removed) == [SID, f"{SID}.jsonl"]
    assert not tp.exists()
    assert not folder.exists()
    assert proj.exists()  # a pasta do PROJETO fica


def test_delete_nao_toca_nada_alen_do_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = _proj(home)
    tp = proj / f"{SID}.jsonl"
    tp.write_text("{}\n", encoding="utf-8")
    outra = proj / f"{OUTRA}.jsonl"  # transcript de OUTRA sessão
    outra.write_text("{}\n", encoding="utf-8")
    (proj / "subagents").mkdir()
    agent = proj / "subagents" / f"agent-{SID}.jsonl"
    agent.write_text("{}\n", encoding="utf-8")

    delete_session_files(_row(str(tp)), home, backup_first=False)

    assert not tp.exists()
    assert outra.exists()
    assert agent.exists()
    assert (proj / "subagents").exists()
    assert proj.exists()


def test_delete_transcript_com_nome_errado_nao_apaga(tmp_path: Path) -> None:
    """transcript_path apontando pra arquivo de OUTRO nome: não mexe nele —
    só remove o que deriva do id da sessão."""
    home = tmp_path / "home"
    proj = _proj(home)
    stranger = proj / "other.jsonl"
    stranger.write_text("{}", encoding="utf-8")
    (proj / SID).mkdir()  # pasta do id existe

    result = delete_session_files(_row(str(stranger)), home, backup_first=False)

    assert stranger.exists()  # nome != id → intocável
    assert not (proj / SID).exists()
    assert [p.name for p in result.removed] == [SID]


def test_delete_backup_antes_de_apagar(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = _proj(home)
    tp = proj / f"{SID}.jsonl"
    tp.write_text(
        '{"type":"user","message":{"role":"user","content":"oi"}}\n', encoding="utf-8"
    )
    dest = tmp_path / "bk"

    result = delete_session_files(_row(str(tp)), home, backup_dest=dest, now=NOW)

    assert not tp.exists()
    assert result.backup_folder is not None
    mds = list(result.backup_folder.glob("*.md"))
    assert len(mds) == 1
    assert "trabalho final" in mds[0].name
    assert "oi" in mds[0].read_text(encoding="utf-8")


def test_delete_backup_falhou_nao_apaga_nada(tmp_path: Path) -> None:
    """Backup pedido que falha aborta a exclusão — nada é apagado."""
    home = tmp_path / "home"
    proj = _proj(home)
    tp = proj / f"{SID}.jsonl"
    tp.write_text("{}\n", encoding="utf-8")
    dest = tmp_path / "arquivo-nao-pasta"
    dest.write_text("x", encoding="utf-8")  # mkdir em cima de arquivo → OSError

    result = delete_session_files(_row(str(tp)), home, backup_dest=dest)

    assert tp.exists()
    assert result.errors
    assert not result.removed


def test_delete_sem_transcript_limpa_pasta_do_id(tmp_path: Path) -> None:
    """transcript já sumiu (status missing): acha a pasta do id mesmo assim."""
    home = tmp_path / "home"
    proj = _proj(home)
    (proj / SID).mkdir()
    (proj / SID / "custom-title.json").write_text("{}", encoding="utf-8")

    result = delete_session_files(_row(""), home, backup_first=False)

    assert [p.name for p in result.removed] == [SID]
    assert not (proj / SID).exists()


def test_session_paths_recusa_id_malicioso(tmp_path: Path) -> None:
    """id com separador/traversal nunca vira caminho a apagar."""
    home = tmp_path / "home"
    _proj(home)
    for bad in ("", ".", "..", "../x", "a/b", "a\\b"):
        assert session_paths(_row(None, sid=bad), home) == []


def test_delete_harness_sem_suporte(tmp_path: Path) -> None:
    row = _row(None)
    row["harness"] = "codex"
    result = delete_session_files(row, tmp_path, backup_first=False)
    assert result.errors
    assert not result.removed
