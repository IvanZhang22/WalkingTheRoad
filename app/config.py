from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值为 {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"环境变量 {name} 必须大于 0")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    thinking: str
    app_mode: str
    timeout_seconds: int
    max_upload_bytes: int
    max_document_chars: int
    provider: str = "deepseek"
    agent_api_key: str = ""

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def agent_key_configured(self) -> bool:
        return bool(self.agent_api_key.strip())


def get_settings() -> Settings:
    provider = os.getenv("MODEL_PROVIDER", "deepseek").strip().lower()
    if provider not in {"stepfun", "deepseek"}:
        raise ValueError("MODEL_PROVIDER 只能是 stepfun 或 deepseek")

    thinking = (
        os.getenv("MODEL_THINKING", os.getenv("DEEPSEEK_THINKING", "disabled")).strip().lower()
    )
    if thinking not in {"enabled", "disabled"}:
        raise ValueError("MODEL_THINKING 只能是 enabled 或 disabled")

    app_mode = os.getenv("APP_MODE", "live").strip().lower()
    if app_mode not in {"live", "mock"}:
        raise ValueError("APP_MODE 只能是 live 或 mock")

    defaults = {
        "stepfun": {
            "base_url": "https://api.stepfun.com/step_plan/v1",
            "model": "step-router-v1",
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        },
    }[provider]

    legacy_api_key = os.getenv("DEEPSEEK_API_KEY", "") if provider == "deepseek" else ""
    legacy_base_url = (
        os.getenv("DEEPSEEK_BASE_URL", defaults["base_url"])
        if provider == "deepseek"
        else defaults["base_url"]
    )
    legacy_model = (
        os.getenv("DEEPSEEK_MODEL", defaults["model"])
        if provider == "deepseek"
        else defaults["model"]
    )

    return Settings(
        api_key=os.getenv("MODEL_API_KEY", legacy_api_key),
        base_url=os.getenv(
            "MODEL_BASE_URL",
            legacy_base_url,
        ).rstrip("/"),
        model=os.getenv("MODEL_NAME", legacy_model),
        thinking=thinking,
        app_mode=app_mode,
        timeout_seconds=_positive_int("MODEL_TIMEOUT_SECONDS", 120),
        max_upload_bytes=_positive_int("MAX_UPLOAD_MB", 20) * 1024 * 1024,
        max_document_chars=_positive_int("MAX_DOCUMENT_CHARS", 300_000),
        provider=provider,
        agent_api_key=os.getenv("AGENT_API_KEY", ""),
    )
