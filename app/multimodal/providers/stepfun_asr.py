"""阶跃星辰异步音频文件识别 Provider。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
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

SUPPORTED_FORMATS = frozenset({"mp3", "wav", "ogg", "pcm"})
PENDING_STATUSES = frozenset({"PENDING", "QUEUED", "RUNNING", "PROCESSING"})
FAILED_STATUSES = frozenset({"FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED"})
Sleep = Callable[[float], Awaitable[None]]


class StepFunASRProvider(ASRProvider):
    """调用 `/audio/asr/file/*`，保留官方分句级毫秒时间戳。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.stepfun.com/v1",
        model: str = "step-asr-1.1",
        request_timeout: float = 30,
        poll_timeout: float = 300,
        poll_interval: float = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if request_timeout <= 0 or poll_timeout <= 0 or poll_interval < 0:
            raise ValueError("阶跃 ASR 超时必须大于 0，轮询间隔不得小于 0")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.request_timeout = request_timeout
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.transport = transport
        self.sleep = sleep

    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        if not self.api_key:
            raise MaterialIngestError(
                "XDW-ASR-NOT-CONFIGURED",
                "阶跃 ASR API Key 尚未配置。",
            )
        if not source.source_url:
            raise MaterialIngestError(
                "XDW-ASR-SOURCE-URL",
                "音频缺少可供识别服务访问的公网地址。",
            )
        suffix = source.source_format or PurePosixPath(source.filename).suffix.lower().lstrip(".")
        if suffix not in SUPPORTED_FORMATS:
            raise MaterialIngestError(
                "XDW-ASR-FORMAT",
                "阶跃文件识别当前只支持 mp3、wav、ogg 或 pcm 音频。",
            )

        timeout = httpx.Timeout(self.request_timeout)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Xingxiaodao-Agent/2.2",
        }
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
        ) as client:
            submitted = await self._post_json(
                client,
                "/audio/asr/file/submit",
                {
                    "audio": {
                        "format": suffix,
                        "url": source.source_url,
                    },
                    "request": {
                        "model_name": self.model,
                        "enable_channel_split": False,
                        "show_utterances": True,
                    },
                },
            )
            task_id = submitted.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise MaterialIngestError(
                    "XDW-ASR-BAD-RESPONSE",
                    "阶跃 ASR 未返回有效任务编号。",
                    retryable=True,
                )
            try:
                async with asyncio.timeout(self.poll_timeout):
                    while True:
                        payload = await self._post_json(
                            client,
                            "/audio/asr/file/query",
                            {"task_id": task_id},
                        )
                        status = str(payload.get("status") or "").upper()
                        if status in PENDING_STATUSES:
                            await self.sleep(self.poll_interval)
                            continue
                        if status in FAILED_STATUSES:
                            raise MaterialIngestError(
                                "XDW-ASR-UPSTREAM-FAILED",
                                "阶跃 ASR 任务处理失败。",
                                retryable=True,
                            )
                        return self._normalize(payload)
            except TimeoutError as exc:
                raise MaterialIngestError(
                    "XDW-ASR-TIMEOUT",
                    "阶跃 ASR 任务等待超时，请稍后重试。",
                    retryable=True,
                ) from exc

    async def _post_json(
        self, client: httpx.AsyncClient, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await client.post(path, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MaterialIngestError(
                "XDW-ASR-TRANSPORT",
                "阶跃 ASR 网络请求失败，请稍后重试。",
                retryable=True,
            ) from exc
        if response.status_code in {401, 403}:
            raise MaterialIngestError(
                "XDW-ASR-AUTH",
                "阶跃 ASR 鉴权失败，请检查服务端环境变量。",
            )
        if not 200 <= response.status_code < 300:
            raise MaterialIngestError(
                "XDW-ASR-UPSTREAM-HTTP",
                f"阶跃 ASR 返回 HTTP {response.status_code}。",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MaterialIngestError(
                "XDW-ASR-BAD-RESPONSE",
                "阶跃 ASR 返回了无法解析的响应。",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise MaterialIngestError(
                "XDW-ASR-BAD-RESPONSE",
                "阶跃 ASR 返回结构无效。",
                retryable=True,
            )
        return payload

    def _normalize(self, payload: dict[str, Any]) -> ProviderResult:
        raw_results = payload.get("result")
        if not isinstance(raw_results, list):
            raise MaterialIngestError(
                "XDW-ASR-BAD-RESPONSE",
                "阶跃 ASR 未返回可用识别结果。",
                retryable=True,
            )

        segments: list[ProviderSegment] = []
        full_texts: list[str] = []
        for channel_index, raw_result in enumerate(raw_results, start=1):
            if not isinstance(raw_result, dict):
                continue
            full_text = _clean_text(raw_result.get("text"))
            if full_text:
                full_texts.append(full_text)
            utterances = raw_result.get("utterances")
            if not isinstance(utterances, list):
                continue
            for utterance in utterances:
                segment = _utterance_segment(utterance, channel_index, len(raw_results))
                if segment is not None:
                    segments.append(segment)

        if not segments:
            raise MaterialIngestError(
                "XDW-ASR-TIMESTAMPS-MISSING",
                "阶跃 ASR 没有返回分句时间戳，结果不能进入证据分析。",
            )
        return ProviderResult(
            provider_name="stepfun",
            provider_model=self.model,
            normalized_text="\n".join(full_texts) or "\n".join(item.text for item in segments),
            segments=segments,
            warnings=["阶跃文件 ASR 官方响应不提供置信度；所有片段需人工复核后才能成为正式证据。"],
        )


def _utterance_segment(
    value: Any, channel_index: int, channel_count: int
) -> ProviderSegment | None:
    if not isinstance(value, dict):
        return None
    text = _clean_text(value.get("text"))
    start_ms = _non_negative_int(value.get("start_time"))
    end_ms = _non_negative_int(value.get("end_time"))
    if not text or start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    return ProviderSegment(
        text=text,
        confidence=None,
        locator=MaterialLocator(start_ms=start_ms, end_ms=end_ms),
        speaker=f"channel_{channel_index}" if channel_count > 1 else None,
    )


def _clean_text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None
