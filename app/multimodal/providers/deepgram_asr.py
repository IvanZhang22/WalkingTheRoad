"""Deepgram 预录音频 ASR Provider。"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import (
    DownloadedFile,
    MaterialLocator,
    ProviderResult,
    ProviderSegment,
)
from app.multimodal.providers.base import ASRProvider


class DeepgramASRProvider(ASRProvider):
    """调用 `/v1/listen`，输出分句时间戳、置信度和说话人。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepgram.com",
        model: str = "nova-3",
        language: str = "zh-CN",
        diarize_model: str = "latest",
        timeout: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("Deepgram ASR 超时必须大于 0")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.language = language.strip()
        self.diarize_model = diarize_model.strip()
        self.timeout = timeout
        self.transport = transport

    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        if not self.api_key:
            raise MaterialIngestError(
                "XDW-ASR-NOT-CONFIGURED",
                "Deepgram ASR API Key 尚未配置。",
            )
        params = {
            "model": self.model,
            "language": self.language,
            "utterances": "true",
            "diarize_model": self.diarize_model,
            "punctuate": "true",
            "smart_format": "true",
        }
        headers = {
            "Authorization": f"Token {self.api_key}",
            "User-Agent": "Xingxiaodao-Agent/2.2",
        }
        request_body: dict[str, Any]
        if source.source_url:
            headers["Content-Type"] = "application/json"
            request_body = {"json": {"url": source.source_url}}
        else:
            headers["Content-Type"] = source.mime_type
            request_body = {"content": await asyncio.to_thread(source.path.read_bytes)}
        return await self._request(params=params, headers=headers, request_body=request_body)

    async def transcribe_url(self, *, url: str, filename: str) -> ProviderResult:
        """让 Deepgram 直接拉取已校验的临时 Blob，避免 Function 再下载大文件。"""

        if not self.api_key:
            raise MaterialIngestError(
                "XDW-ASR-NOT-CONFIGURED",
                "Deepgram ASR API Key 尚未配置。",
            )
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in {"mp3", "wav", "m4a", "webm"}:
            raise MaterialIngestError("XDW-FILE-TYPE", "当前音频文件类型不受支持。")
        return await self._request(
            params={
                "model": self.model,
                "language": self.language,
                "utterances": "true",
                "diarize_model": self.diarize_model,
                "punctuate": "true",
                "smart_format": "true",
            },
            headers={
                "Authorization": f"Token {self.api_key}",
                "User-Agent": "Xingxiaodao-Agent/2.2",
                "Content-Type": "application/json",
            },
            request_body={"json": {"url": url}},
        )

    async def _request(
        self,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        request_body: dict[str, Any],
    ) -> ProviderResult:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                transport=self.transport,
                trust_env=False,
                headers=headers,
            ) as client:
                response = await client.post("/v1/listen", params=params, **request_body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MaterialIngestError(
                "XDW-ASR-TRANSPORT",
                "Deepgram ASR 网络请求失败，请稍后重试。",
                retryable=True,
            ) from exc
        if response.status_code in {401, 403}:
            raise MaterialIngestError(
                "XDW-ASR-AUTH",
                "Deepgram ASR 鉴权失败，请检查服务端环境变量。",
            )
        if not 200 <= response.status_code < 300:
            raise MaterialIngestError(
                "XDW-ASR-UPSTREAM-HTTP",
                f"Deepgram ASR 返回 HTTP {response.status_code}。",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MaterialIngestError(
                "XDW-ASR-BAD-RESPONSE",
                "Deepgram ASR 返回了无法解析的响应。",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise MaterialIngestError(
                "XDW-ASR-BAD-RESPONSE",
                "Deepgram ASR 返回结构无效。",
                retryable=True,
            )
        return self._normalize(payload)

    def _normalize(self, payload: dict[str, Any]) -> ProviderResult:
        segments = _utterance_segments(payload)
        warnings: list[str] = []
        if not segments:
            segments = _alternative_fallback(payload)
            if segments:
                warnings.append("DEEPGRAM_UTTERANCES_MISSING")
        if not segments:
            raise MaterialIngestError(
                "XDW-ASR-EMPTY",
                "Deepgram ASR 没有返回带有效时间戳的转写。",
            )
        return ProviderResult(
            provider_name="deepgram",
            provider_model=_provider_model(payload) or self.model,
            normalized_text="\n".join(segment.text for segment in segments),
            segments=segments,
            warnings=warnings,
        )


def _utterance_segments(payload: dict[str, Any]) -> list[ProviderSegment]:
    results = payload.get("results")
    if not isinstance(results, dict) or not isinstance(results.get("utterances"), list):
        return []
    segments: list[ProviderSegment] = []
    for item in results["utterances"]:
        if not isinstance(item, dict):
            continue
        segment = _segment(
            text=item.get("transcript"),
            start=item.get("start"),
            end=item.get("end"),
            confidence=item.get("confidence"),
            words=item.get("words"),
            speaker=item.get("speaker"),
        )
        if segment is not None:
            segments.append(segment)
    return segments


def _alternative_fallback(payload: dict[str, Any]) -> list[ProviderSegment]:
    results = payload.get("results")
    channels = results.get("channels") if isinstance(results, dict) else None
    if not isinstance(channels, list) or not channels or not isinstance(channels[0], dict):
        return []
    alternatives = channels[0].get("alternatives")
    if (
        not isinstance(alternatives, list)
        or not alternatives
        or not isinstance(alternatives[0], dict)
    ):
        return []
    alternative = alternatives[0]
    words = alternative.get("words")
    word_rows = (
        [item for item in words if isinstance(item, dict)] if isinstance(words, list) else []
    )
    metadata = payload.get("metadata")
    duration = metadata.get("duration") if isinstance(metadata, dict) else None
    segment = _segment(
        text=alternative.get("transcript"),
        start=word_rows[0].get("start") if word_rows else 0,
        end=word_rows[-1].get("end") if word_rows else duration,
        confidence=alternative.get("confidence"),
        words=word_rows,
        speaker=word_rows[0].get("speaker") if word_rows else None,
    )
    return [segment] if segment is not None else []


def _segment(
    *,
    text: Any,
    start: Any,
    end: Any,
    confidence: Any,
    words: Any,
    speaker: Any,
) -> ProviderSegment | None:
    cleaned = text.strip() if isinstance(text, str) else ""
    start_seconds = _finite(start)
    end_seconds = _finite(end)
    if not cleaned or start_seconds is None or end_seconds is None:
        return None
    start_ms = round(start_seconds * 1000)
    end_ms = round(end_seconds * 1000)
    if start_ms < 0 or end_ms <= start_ms:
        return None
    confidence_value = _confidence(confidence)
    word_rows = (
        [item for item in words if isinstance(item, dict)] if isinstance(words, list) else []
    )
    if confidence_value is None:
        word_confidences = [
            value
            for item in word_rows
            if (value := _confidence(item.get("confidence"))) is not None
        ]
        if word_confidences:
            confidence_value = round(sum(word_confidences) / len(word_confidences), 6)
    if speaker is None and word_rows:
        speaker = word_rows[0].get("speaker")
    speaker_label = None if speaker is None or isinstance(speaker, bool) else f"speaker_{speaker}"
    return ProviderSegment(
        text=cleaned,
        confidence=confidence_value,
        locator=MaterialLocator(start_ms=start_ms, end_ms=end_ms),
        speaker=speaker_label,
    )


def _provider_model(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    model_info = metadata.get("model_info") if isinstance(metadata, dict) else None
    if not isinstance(model_info, dict) or not model_info:
        return None
    first = next(iter(model_info.values()))
    name = first.get("name") if isinstance(first, dict) else None
    return str(name) if isinstance(name, str) and name.strip() else None


def _finite(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _confidence(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and 0 <= number <= 1 else None
