from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile
from app.multimodal.providers.deepgram_asr import DeepgramASRProvider


def source(tmp_path: Path) -> DownloadedFile:
    path = tmp_path / "interview.m4a"
    path.write_bytes(b"audio")
    return DownloadedFile(
        path=path,
        source_url="https://files.example.org/interview.m4a?signature=secret",
        source_format="m4a",
        filename="interview.m4a",
        mime_type="audio/mp4",
        size_bytes=5,
        sha256="c" * 64,
    )


async def test_maps_utterances_to_timestamped_confident_segments(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["query"] = dict(request.url.params)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "metadata": {"model_info": {"id": {"name": "nova-3"}}},
                "results": {
                    "utterances": [
                        {
                            "start": 1.25,
                            "end": 3.5,
                            "confidence": 0.96,
                            "transcript": "真实语音转写",
                            "speaker": 0,
                            "words": [],
                        },
                        {
                            "start": 3.6,
                            "end": 5.0,
                            "transcript": "第二个片段",
                            "words": [
                                {"confidence": 0.8, "speaker": 1},
                                {"confidence": 1.0, "speaker": 1},
                            ],
                        },
                    ]
                },
            },
        )

    result = await DeepgramASRProvider(
        api_key="test-key", transport=httpx.MockTransport(handler)
    ).transcribe(source(tmp_path))

    assert captured["body"] == {"url": "https://files.example.org/interview.m4a?signature=secret"}
    assert captured["query"] == {
        "model": "nova-3",
        "language": "zh-CN",
        "utterances": "true",
        "diarize_model": "latest",
        "punctuate": "true",
        "smart_format": "true",
    }
    assert captured["headers"]["authorization"] == "Token test-key"  # type: ignore[index]
    assert result.provider_name == "deepgram"
    assert result.provider_model == "nova-3"
    assert result.segments[0].locator.start_ms == 1250
    assert result.segments[0].locator.end_ms == 3500
    assert result.segments[0].confidence == 0.96
    assert result.segments[0].speaker == "speaker_0"
    assert result.segments[1].confidence == 0.9
    assert result.segments[1].speaker == "speaker_1"


async def test_uploads_local_audio_bytes_when_source_url_is_absent(tmp_path: Path) -> None:
    local = source(tmp_path)
    local.source_url = None
    local.path.write_bytes(b"local-m4a-bytes")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "results": {
                    "utterances": [
                        {
                            "start": 0.1,
                            "end": 1.0,
                            "confidence": 0.95,
                            "transcript": "本地音频真实转写",
                            "words": [],
                        }
                    ]
                }
            },
        )

    result = await DeepgramASRProvider(
        api_key="test-key", transport=httpx.MockTransport(handler)
    ).transcribe(local)

    assert captured == {
        "content_type": "audio/mp4",
        "body": b"local-m4a-bytes",
    }
    assert result.normalized_text == "本地音频真实转写"


async def test_falls_back_to_channel_alternative(tmp_path: Path) -> None:
    response = {
        "metadata": {"duration": 2.0},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "fallback transcript",
                            "confidence": 0.91,
                            "words": [
                                {"start": 0.1, "end": 0.8, "speaker": 0},
                                {"start": 0.9, "end": 1.7, "speaker": 0},
                            ],
                        }
                    ]
                }
            ]
        },
    }
    provider = DeepgramASRProvider(
        api_key="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    )
    result = await provider.transcribe(source(tmp_path))
    assert result.segments[0].locator.start_ms == 100
    assert result.segments[0].locator.end_ms == 1700
    assert "DEEPGRAM_UTTERANCES_MISSING" in result.warnings


async def test_missing_key_and_empty_result_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(MaterialIngestError) as missing_key:
        await DeepgramASRProvider(api_key="").transcribe(source(tmp_path))
    assert missing_key.value.code == "XDW-ASR-NOT-CONFIGURED"

    empty = DeepgramASRProvider(
        api_key="test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": {"utterances": []}})
        ),
    )
    with pytest.raises(MaterialIngestError) as no_segments:
        await empty.transcribe(source(tmp_path))
    assert no_segments.value.code == "XDW-ASR-EMPTY"


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    [
        (401, "XDW-ASR-AUTH", False),
        (429, "XDW-ASR-UPSTREAM-HTTP", True),
        (500, "XDW-ASR-UPSTREAM-HTTP", True),
    ],
)
async def test_http_errors_do_not_expose_key_url_or_vendor_body(
    tmp_path: Path, status_code: int, code: str, retryable: bool
) -> None:
    provider = DeepgramASRProvider(
        api_key="test-secret-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"error": "secret vendor detail"})
        ),
    )
    with pytest.raises(MaterialIngestError) as caught:
        await provider.transcribe(source(tmp_path))
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "test-secret-key" not in caught.value.public_message
    assert "signature" not in caught.value.public_message
    assert "secret vendor detail" not in caught.value.public_message
