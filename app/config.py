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


def _non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值为 {raw!r}") from exc
    if value < 0:
        raise ValueError(f"环境变量 {name} 不得小于 0")
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
    multimodal_connect_timeout_seconds: int = 10
    multimodal_read_timeout_seconds: int = 120
    multimodal_max_redirects: int = 3
    asr_provider: str = "disabled"
    stepfun_asr_api_key: str = ""
    stepfun_asr_base_url: str = "https://api.stepfun.com/v1"
    stepfun_asr_model: str = "step-asr-1.1"
    stepfun_asr_request_timeout_seconds: int = 30
    stepfun_asr_poll_timeout_seconds: int = 300
    stepfun_asr_poll_interval_seconds: int = 2
    deepgram_api_key: str = ""
    deepgram_base_url: str = "https://api.deepgram.com"
    deepgram_model: str = "nova-3"
    deepgram_language: str = "zh-CN"
    deepgram_diarize_model: str = "latest"
    deepgram_timeout_seconds: int = 120
    ocr_provider: str = "disabled"
    baidu_ocr_api_key: str = ""
    baidu_ocr_secret_key: str = ""
    baidu_ocr_base_url: str = "https://aip.baidubce.com"
    baidu_ocr_endpoint_path: str = "/rest/2.0/ocr/v1/general"
    baidu_ocr_timeout_seconds: int = 60
    baidu_ocr_max_pages: int = 20

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def agent_key_configured(self) -> bool:
        return bool(self.agent_api_key.strip())

    @property
    def stepfun_asr_key_configured(self) -> bool:
        return bool(self.stepfun_asr_api_key.strip())

    @property
    def deepgram_key_configured(self) -> bool:
        return bool(self.deepgram_api_key.strip())

    @property
    def asr_key_configured(self) -> bool:
        if self.asr_provider == "stepfun":
            return self.stepfun_asr_key_configured
        if self.asr_provider == "deepgram":
            return self.deepgram_key_configured
        return False

    @property
    def baidu_ocr_key_configured(self) -> bool:
        return bool(self.baidu_ocr_api_key.strip() and self.baidu_ocr_secret_key.strip())


def get_settings() -> Settings:
    provider = os.getenv("MODEL_PROVIDER", "deepseek").strip().lower()
    if provider not in {"stepfun", "deepseek", "vercel"}:
        raise ValueError("MODEL_PROVIDER 只能是 stepfun、deepseek 或 vercel")

    thinking = (
        os.getenv("MODEL_THINKING", os.getenv("DEEPSEEK_THINKING", "disabled")).strip().lower()
    )
    if thinking not in {"enabled", "disabled"}:
        raise ValueError("MODEL_THINKING 只能是 enabled 或 disabled")

    app_mode = os.getenv("APP_MODE", "live").strip().lower()
    if app_mode not in {"live", "mock"}:
        raise ValueError("APP_MODE 只能是 live 或 mock")

    asr_provider = os.getenv("ASR_PROVIDER", "disabled").strip().lower()
    if asr_provider not in {"disabled", "stepfun", "deepgram"}:
        raise ValueError("ASR_PROVIDER 只能是 disabled、stepfun 或 deepgram")

    ocr_provider = os.getenv("OCR_PROVIDER", "disabled").strip().lower()
    if ocr_provider not in {"disabled", "baidu"}:
        raise ValueError("OCR_PROVIDER 只能是 disabled 或 baidu")

    defaults = {
        "stepfun": {
            "base_url": "https://api.stepfun.com/step_plan/v1",
            "model": "step-router-v1",
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        },
        "vercel": {
            "base_url": "https://ai-gateway.vercel.sh/v1",
            "model": "openai/gpt-5.4-mini",
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

    explicit_model_key = os.getenv("MODEL_API_KEY", "").strip()
    if provider == "vercel":
        model_api_key = (
            explicit_model_key
            or os.getenv("AI_GATEWAY_API_KEY", "").strip()
            or os.getenv("VERCEL_OIDC_TOKEN", "").strip()
        )
    else:
        model_api_key = explicit_model_key or legacy_api_key
    stepfun_key_fallback = model_api_key if provider == "stepfun" else ""

    return Settings(
        api_key=model_api_key,
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
        multimodal_connect_timeout_seconds=_positive_int("MULTIMODAL_CONNECT_TIMEOUT_SECONDS", 10),
        multimodal_read_timeout_seconds=_positive_int("MULTIMODAL_READ_TIMEOUT_SECONDS", 120),
        multimodal_max_redirects=_non_negative_int("MULTIMODAL_MAX_REDIRECTS", 3),
        asr_provider=asr_provider,
        stepfun_asr_api_key=(os.getenv("STEPFUN_ASR_API_KEY", "").strip() or stepfun_key_fallback),
        stepfun_asr_base_url=os.getenv("STEPFUN_ASR_BASE_URL", "https://api.stepfun.com/v1").rstrip(
            "/"
        ),
        stepfun_asr_model=os.getenv("STEPFUN_ASR_MODEL", "step-asr-1.1"),
        stepfun_asr_request_timeout_seconds=_positive_int(
            "STEPFUN_ASR_REQUEST_TIMEOUT_SECONDS", 30
        ),
        stepfun_asr_poll_timeout_seconds=_positive_int("STEPFUN_ASR_POLL_TIMEOUT_SECONDS", 300),
        stepfun_asr_poll_interval_seconds=_non_negative_int("STEPFUN_ASR_POLL_INTERVAL_SECONDS", 2),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        deepgram_base_url=os.getenv("DEEPGRAM_BASE_URL", "https://api.deepgram.com").rstrip("/"),
        deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
        deepgram_language=os.getenv("DEEPGRAM_LANGUAGE", "zh-CN"),
        deepgram_diarize_model=os.getenv("DEEPGRAM_DIARIZE_MODEL", "latest"),
        deepgram_timeout_seconds=_positive_int("DEEPGRAM_TIMEOUT_SECONDS", 120),
        ocr_provider=ocr_provider,
        baidu_ocr_api_key=os.getenv("BAIDU_OCR_API_KEY", ""),
        baidu_ocr_secret_key=os.getenv("BAIDU_OCR_SECRET_KEY", ""),
        baidu_ocr_base_url=os.getenv("BAIDU_OCR_BASE_URL", "https://aip.baidubce.com").rstrip("/"),
        baidu_ocr_endpoint_path=os.getenv("BAIDU_OCR_ENDPOINT_PATH", "/rest/2.0/ocr/v1/general"),
        baidu_ocr_timeout_seconds=_positive_int("BAIDU_OCR_TIMEOUT_SECONDS", 60),
        baidu_ocr_max_pages=_positive_int("BAIDU_OCR_MAX_PAGES", 20),
    )
