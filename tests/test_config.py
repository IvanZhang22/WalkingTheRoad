from __future__ import annotations

import pytest

from app.config import get_settings


def test_stepfun_asr_can_reuse_stepfun_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "stepfun")
    monkeypatch.setenv("MODEL_API_KEY", "shared-test-key")
    monkeypatch.setenv("ASR_PROVIDER", "stepfun")
    monkeypatch.setenv("STEPFUN_ASR_API_KEY", "")

    settings = get_settings()

    assert settings.stepfun_asr_api_key == "shared-test-key"
    assert settings.stepfun_asr_key_configured is True


def test_stepfun_asr_does_not_reuse_another_vendor_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("MODEL_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("ASR_PROVIDER", "stepfun")
    monkeypatch.setenv("STEPFUN_ASR_API_KEY", "")

    settings = get_settings()

    assert settings.stepfun_asr_api_key == ""
    assert settings.stepfun_asr_key_configured is False


def test_rejects_unknown_asr_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="ASR_PROVIDER"):
        get_settings()
