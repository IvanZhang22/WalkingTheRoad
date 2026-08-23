"""StepFun SSE ASR provider.

Unlike the legacy file-ASR endpoint this sends each small, normalised segment
directly to StepFun.  It deliberately never requires StepFun to fetch a
temporary URL from our server, which was the unstable link in v3.3.x.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile, MaterialLocator, ProviderResult, ProviderSegment
from app.multimodal.providers.base import ASRProvider


class StepFunSSEASRProvider(ASRProvider):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.stepfun.com/step_plan/v1",
        model: str = "stepaudio-2.5-asr",
        timeout: float = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.timeout = timeout
        self.transport = transport

    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        if not self.api_key:
            raise MaterialIngestError("XDW-ASR-NOT-CONFIGURED", "音频转写服务尚未配置。")
        payload = base64.b64encode(source.path.read_bytes()).decode("ascii")
        # Exact schema from StepFun SSE ASR: audio.data plus audio.input.
        # ``model`` and ``enable_timestamp`` are deliberately nested under
        # transcription; top-level variants are rejected by the API.
        body = {
            "audio": {
                "data": payload,
                "input": {
                    "transcription": {
                        "language": "zh",
                        "model": self.model,
                        "enable_itn": True,
                        "enable_timestamp": True,
                    },
                    "format": {"type": (source.source_format or "mp3").lower()},
                },
            }
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        texts: list[str] = []
        segments: list[ProviderSegment] = []
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
                transport=self.transport,
                trust_env=False,
            ) as client:
                async with client.stream("POST", "/audio/asr/sse", json=body) as response:
                    if response.status_code in {401, 403}:
                        raise MaterialIngestError("XDW-ASR-AUTH", "音频转写服务鉴权失败。")
                    if not 200 <= response.status_code < 300:
                        raise MaterialIngestError(
                            "XDW-ASR-UPSTREAM-HTTP",
                            f"音频转写服务返回 HTTP {response.status_code}。",
                            retryable=response.status_code >= 500 or response.status_code == 429,
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw in {"[DONE]", ""}:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "error":
                            message = str(event.get("message") or "上游未说明原因")[:300]
                            raise MaterialIngestError(
                                "XDW-ASR-UPSTREAM-EVENT",
                                f"音频转写服务报错：{message}",
                                retryable=True,
                            )
                        self._collect(event, texts, segments)
        except MaterialIngestError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            raise MaterialIngestError(
                "XDW-ASR-TRANSPORT", "音频转写网络请求失败，请稍后重试。", retryable=True
            ) from exc
        text = "\n".join(dict.fromkeys(item.strip() for item in texts if item.strip()))
        if not text and segments:
            text = "\n".join(item.text for item in segments)
        if not text:
            raise MaterialIngestError("XDW-ASR-EMPTY", "音频转写未返回可用文本。", retryable=True)
        if not segments:
            segments = [ProviderSegment(text=text, locator=MaterialLocator())]
        return ProviderResult(
            provider_name="stepfun_sse",
            provider_model=self.model,
            normalized_text=text,
            segments=segments,
            warnings=["转写由分段 SSE 服务生成；正式引用前仍应抽样核对原音频。"],
        )

    @staticmethod
    def _collect(event: dict[str, Any], texts: list[str], segments: list[ProviderSegment]) -> None:
        # StepFun has used both direct fields and OpenAI-like delta wrappers.
        data: dict[str, Any] = event
        if isinstance(event.get("delta"), str):
            text = str(event["delta"]).strip()
            if text:
                texts.append(text)
                start = event.get("start_time")
                end = event.get("end_time")
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    segments.append(
                        ProviderSegment(
                            text=text,
                            locator=MaterialLocator(
                                start_ms=max(0, int(start)), end_ms=max(0, int(end))
                            ),
                        )
                    )
            return
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            data = choices[0].get("delta") or choices[0].get("message") or choices[0]
            if not isinstance(data, dict):
                return
        value: Any = data.get("text") or data.get("content") or data.get("transcript")
        text = value if isinstance(value, str) else ""
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
        start = data.get("start_ms", data.get("start_time"))
        end = data.get("end_ms", data.get("end_time"))
        if (
            isinstance(text, str)
            and text.strip()
            and isinstance(start, (int, float))
            and isinstance(end, (int, float))
        ):
            segments.append(
                ProviderSegment(
                    text=text.strip(),
                    locator=MaterialLocator(start_ms=max(0, int(start)), end_ms=max(0, int(end))),
                )
            )
