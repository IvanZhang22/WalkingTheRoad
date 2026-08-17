from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import app.main as main_module


def test_conversation_database_defaults_to_project_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("CONVERSATION_DATABASE_PATH", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)

    assert main_module._conversation_database_path() == (
        tmp_path / "data" / "qingxiaoda_conversations.sqlite3"
    )


def test_conversation_database_uses_temp_directory_on_vercel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONVERSATION_DATABASE_PATH", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    assert main_module._conversation_database_path() == (
        Path(tempfile.gettempdir()) / "xingxiaodao" / "qingxiaoda_conversations.sqlite3"
    )


def test_conversation_database_explicit_path_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_path = tmp_path / "state" / "conversation.sqlite3"
    monkeypatch.setenv("CONVERSATION_DATABASE_PATH", str(configured_path))
    monkeypatch.setenv("VERCEL", "1")

    assert main_module._conversation_database_path() == configured_path
