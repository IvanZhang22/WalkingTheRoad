from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_qingxiaoda_conversation_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep session state from one test run out of every other test run.

    Local runs store Qingxiaoda conversations under the project data directory.
    Tests use pytest's per-test temporary root so a reused sessionId cannot make
    a second pytest invocation inherit an earlier conversation state.
    """

    monkeypatch.setattr("app.main.PROJECT_ROOT", tmp_path)
