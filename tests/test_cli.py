"""Testes da CLI (CliRunner, tudo isolado em tmp)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from helpers import make_assistant_line, make_user_line, write_transcript
from typer.testing import CliRunner

from sessionhub.cli import app

SID = "11111111-2222-3333-4444-555555555555"
runner = CliRunner()


def _setup_env(monkeypatch, fake_home: Path, tmp_path: Path) -> Path:
    monkeypatch.setenv("SESSIONHUB_HOME", str(tmp_path / "sh"))
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return tmp_path / "sh"


def test_cli_scan_ls_json(fake_home: Path, git_repo: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(git_repo), "título de teste")],
    )
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["harness"] == "claude"
    assert data[0]["title"] == "título de teste"
    assert data[0]["resume_command"] == f"claude --resume {SID}"
    assert data[0]["worktree_exists"] is True
    assert data[0]["branch"] == "main"


def test_cli_ls_tabela(fake_home: Path, git_repo: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(git_repo), "tabela visível")],
    )
    assert runner.invoke(app, ["scan"]).exit_code == 0
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "tabela visível" in result.output
    assert "claude" in result.output


def test_cli_ls_status_invalido(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSIONHUB_HOME", str(tmp_path / "sh"))
    result = runner.invoke(app, ["ls", "--status", "xablau"])
    assert result.exit_code == 2


def test_cli_ls_vazio(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSIONHUB_HOME", str(tmp_path / "sh"))
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "scan" in result.output  # dica para escanear


def test_cli_open_dry_run(fake_home: Path, git_repo: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, str(git_repo), "abrir")],
    )
    assert runner.invoke(app, ["scan"]).exit_code == 0
    result = runner.invoke(app, ["open", SID[:8], "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "claude --resume" in result.output


def test_cli_open_prefixo_ambiguo(fake_home: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(projects / "aaaa1.jsonl", [make_user_line("aaaa1", "Z:\\x", "a")])
    write_transcript(projects / "aaaa2.jsonl", [make_user_line("aaaa2", "Z:\\x", "b")])
    assert runner.invoke(app, ["scan"]).exit_code == 0
    result = runner.invoke(app, ["open", "aaaa"])
    assert result.exit_code == 2
    assert "ambíguo" in result.output


def test_cli_open_nao_encontrado(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSIONHUB_HOME", str(tmp_path / "sh"))
    result = runner.invoke(app, ["open", "xyz"])
    assert result.exit_code == 1


def test_cli_open_worktree_sumiu(fake_home: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    gone = tmp_path / "gone-repo"
    gone.mkdir()
    projects = fake_home / ".claude" / "projects" / "G--gone"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, str(gone), "x")])
    assert runner.invoke(app, ["scan"]).exit_code == 0
    # apaga o cwd depois do scan
    import shutil

    shutil.rmtree(gone)
    result = runner.invoke(app, ["open", SID])
    assert result.exit_code == 0
    assert "não existe" in result.output
    assert "claude --resume" in result.output


def test_cli_doctor(fake_home: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    assert "codex" in result.output
    assert "gemini" in result.output


def test_cli_scan_full(fake_home: Path, git_repo: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(projects / f"{SID}.jsonl", [make_user_line(SID, str(git_repo), "full")])
    assert runner.invoke(app, ["scan"]).exit_code == 0
    result = runner.invoke(app, ["scan", "--full"])
    assert result.exit_code == 0, result.output
    assert os.path.isfile(tmp_path / "sh" / "index.db")


def test_cli_show_historico(fake_home: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [
            make_user_line(SID, "G:\\repo", "o que foi digitado primeiro", ts="2026-11-01T10:00Z"),
            make_assistant_line(SID, "G:\\repo", "resposta antiga", "2026-11-01T10:01Z"),
            make_user_line(SID, "G:\\repo", "última pergunta", ts="2026-11-01T11:00Z"),
        ],
    )
    assert runner.invoke(app, ["scan"]).exit_code == 0
    result = runner.invoke(app, ["show", SID[:8]])
    assert result.exit_code == 0, result.output
    assert "o que foi digitado primeiro" in result.output
    assert "última pergunta" in result.output
    assert "👤 você" in result.output
    assert "🤖 agente" in result.output
    assert f"claude --resume {SID}" in result.output


def test_cli_show_apenas_pedido_inicial(fake_home: Path, tmp_path: Path, monkeypatch) -> None:
    _setup_env(monkeypatch, fake_home, tmp_path)
    projects = fake_home / ".claude" / "projects" / "G--repo"
    write_transcript(
        projects / f"{SID}.jsonl",
        [make_user_line(SID, "G:\\repo", "só o começo")],
    )
    assert runner.invoke(app, ["scan"]).exit_code == 0
    result = runner.invoke(app, ["show", SID[:8], "-n", "0"])
    assert result.exit_code == 0, result.output
    assert "só o começo" in result.output
    assert "pedido inicial" in result.output


def test_cli_show_nao_encontrado(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSIONHUB_HOME", str(tmp_path / "sh"))
    result = runner.invoke(app, ["show", "xyz"])
    assert result.exit_code == 1
