from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile
from app.multimodal.providers.stepfun_asr import StepFunASRProvider


def source(tmp_path: Path, *, filename: str = "interview.mp3") -> DownloadedFile:
    path = tmp_path / filename
    path.write_bytes(b"ID3-test")
    return DownloadedFile(
        path=path,
        source_url=f"https://files.example.org/{filename}?signature=secret",
        source_format=Path(filename).suffix.lower().lstrip("."),
        filename=filename,
        mime_type="audio/mpeg",
        size_bytes=8,
        sha256="a" * 64,
    )


async def no_sleep(seconds: float) -> None:
    return None


async def test_submits_polls_and_normalizes_timestamped_utterances(tmp_path: Path) -> None:
    requests: list[tuple[str, dict[str, object], str]] = []
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        body = json.loads(request.content)
        requests.append((request.url.path, body, request.headers["authorization"]))
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={"task_id": "task-1"})
        query_count += 1
        if query_count == 1:
            return httpx.Response(200, json={"status": "RUNNING"})
        return httpx.Response(
            200,
            json={
                "duration": 3.2,
                "result": [
                    {
                        "text": "第一句话。第二句话。",
                        "utterances": [
                            {
                                "text": "第一句话。",
                                "start_time": 0,
                                "end_time": 1200,
                                "words": [],
                            },
                            {
                                "text": "第二句话。",
                                "start_time": 1300,
                                "end_time": 3000,
                                "words": [],
                            },
                        ],
                    }
                ],
            },
        )

    provider = StepFunASRProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        poll_interval=0,
        sleep=no_sleep,
    )
    result = await provider.transcribe(source(tmp_path))

    assert result.provider_name == "stepfun"
    assert result.provider_model == "step-asr-1.1"
    assert result.normalized_text == "第一句话。第二句话。"
    assert [(item.locator.start_ms, item.locator.end_ms) for item in result.segments] == [
        (0, 1200),
        (1300, 3000),
    ]
    assert all(item.confidence is None for item in result.segments)
    assert requests[0][0] == "/v1/audio/asr/file/submit"
    assert requests[0][1] == {
        "audio": {
            "format": "mp3",
            "url": "https://files.example.org/interview.mp3?signature=secret",
        },
        "request": {
            "model_name": "step-asr-1.1",
            "enable_channel_split": False,
            "show_utterances": True,
        },
    }
    assert requests[1][1] == {"task_id": "task-1"}
    assert all(item[2] == "Bearer test-key" for item in requests)


async def test_rejects_missing_key_unsupported_format_and_missing_url(tmp_path: Path) -> None:
    with pytest.raises(MaterialIngestError) as missing_key:
        await StepFunASRProvider(api_key="").transcribe(source(tmp_path))
    assert missing_key.value.code == "XDW-ASR-NOT-CONFIGURED"

    with pytest.raises(MaterialIngestError) as unsupported:
        await StepFunASRProvider(api_key="test").transcribe(
            source(tmp_path, filename="interview.m4a")
        )
    assert unsupported.value.code == "XDW-ASR-FORMAT"

    missing_url = source(tmp_path)
    missing_url.source_url = None
    with pytest.raises(MaterialIngestError) as no_url:
        await StepFunASRProvider(api_key="test").transcribe(missing_url)
    assert no_url.value.code == "XDW-ASR-SOURCE-URL"


@pytest.mark.parametrize(
    ("status_code", "response", "code", "retryable"),
    [
        (401, {"error": "secret detail"}, "XDW-ASR-AUTH", False),
        (429, {"error": "secret detail"}, "XDW-ASR-UPSTREAM-HTTP", True),
        (500, {"error": "secret detail"}, "XDW-ASR-UPSTREAM-HTTP", True),
        (200, {"unexpected": "secret detail"}, "XDW-ASR-BAD-RESPONSE", True),
    ],
)
async def test_safe_upstream_failures_do_not_expose_body(
    tmp_path: Path,
    status_code: int,
    response: dict[str, str],
    code: str,
    retryable: bool,
) -> None:
    provider = StepFunASRProvider(
        api_key="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json=response)),
    )
    with pytest.raises(MaterialIngestError) as caught:
        await provider.transcribe(source(tmp_path))
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "secret detail" not in caught.value.public_message
    assert "signature" not in caught.value.public_message


async def test_failed_task_fails_closed(tmp_path: Path) -> None:
    query_payload = {"status": "FAILED", "message": "secret"}
    expected_code = "XDW-ASR-UPSTREAM-FAILED"

    def handler(
        request: httpx.Request, result: dict[str, object] = query_payload
    ) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={"task_id": "task-1"})
        return httpx.Response(200, json=result)

    provider = StepFunASRProvider(
        api_key="test",
        transport=httpx.MockTransport(handler),
        poll_interval=0,
        sleep=no_sleep,
    )
    with pytest.raises(MaterialIngestError) as caught:
        await provider.transcribe(source(tmp_path))
    assert caught.value.code == expected_code
    assert "secret" not in caught.value.public_message


async def test_full_transcript_without_timestamps_is_manual_review(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={"task_id": "task-1"})
        return httpx.Response(200, json={"result": [{"text": "只有整段文本"}]})

    provider = StepFunASRProvider(
        api_key="test",
        transport=httpx.MockTransport(handler),
        poll_interval=0,
        sleep=no_sleep,
    )
    result = await provider.transcribe(source(tmp_path))
    assert result.normalized_text == "只有整段文本"
    assert len(result.segments) == 1
    assert result.segments[0].locator.start_ms is None
    assert result.warnings


async def test_downloaded_file_never_serializes_source_url(tmp_path: Path) -> None:
    downloaded = source(tmp_path)
    assert "source_url" not in downloaded.model_dump()
    assert "signature" not in downloaded.model_dump_json()
