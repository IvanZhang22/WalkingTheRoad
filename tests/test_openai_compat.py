from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import MockLLMClient
from app.main import create_app
from app.multimodal.downloader import MockDownloader
from app.multimodal.models import MaterialModality
from app.multimodal.providers.document import LocalDocumentParser
from app.multimodal.providers.mock import MockASRProvider, MockOCRProvider
from app.multimodal.service import MaterialIngestService


def settings() -> Settings:
    return Settings(
        api_key="",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking="disabled",
        app_mode="mock",
        timeout_seconds=120,
        max_upload_bytes=20 * 1024 * 1024,
        max_document_chars=300_000,
        agent_api_key="test-agent-key",
    )


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-agent-key"}


def test_models_requires_separate_agent_key() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        denied = client.get("/v1/models")
        assert denied.status_code == 401
        assert denied.json()["detail"]["error"]["code"] == "invalid_api_key"

        response = client.get("/v1/models", headers=headers())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json; charset=utf-8")
        assert response.json()["object"] == "list"
        assert response.json()["data"][0]["id"] == "xingxiaodao-agent"


def test_non_streaming_chat_returns_openai_shape_and_route() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "",
                "messages": [{"role": "user", "content": "我有三份访谈记录，想做主题分析"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "xingxiaodao-agent"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert "W3 质性材料分析" in body["choices"][0]["message"]["content"]
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] >= 2


def test_streaming_chat_ends_with_stop_usage_and_done() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "model": "xingxiaodao-agent",
                "stream": True,
                "messages": [{"role": "user", "content": "帮我设计访谈提纲"}],
            },
        )
        assert response.status_code == 200
        frames = [line.removeprefix("data: ") for line in response.text.splitlines() if line]
        assert frames[-1] == "[DONE]"
        chunks = [json.loads(frame) for frame in frames[:-1]]
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert "W2 访谈设计助手" in chunks[1]["choices"][0]["delta"]["content"]
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert "usage" in chunks[-1]


def test_probe_and_input_contract() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        probe = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 1,
            },
        )
        assert probe.status_code == 200
        assert probe.json()["choices"][0]["message"]["content"] == "行小道服务正常。"

        content_parts = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={"messages": [{"role": "user", "content": [{"type": "text", "text": "测试"}]}]},
        )
        assert content_parts.status_code == 200


def test_mock_multimodal_parts_use_same_chat_endpoint_without_echoing_url() -> None:
    signed_url = "https://files.example.org/interview.mp3?token=must-not-echo"
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请进入质性材料分析并提取主题"},
                            {
                                "type": "input_audio",
                                "input_audio": {"url": signed_url, "format": "mp3"},
                            },
                            {
                                "type": "file",
                                "file": {
                                    "url": "https://files.example.org/note.docx",
                                    "filename": "观察笔记.docx",
                                },
                            },
                        ],
                    }
                ]
            },
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert "W3 质性材料分析" in content
        assert "共 2 份" in content
        assert "自动可用 2 份" in content
        assert "# 3L-3-2 模拟结果" in content
        assert "MAT_" in content
        assert signed_url not in content
        assert "must-not-echo" not in content


def test_live_audio_fails_per_material_without_calling_unconfigured_provider() -> None:
    live_settings = replace(settings(), app_mode="live")
    with TestClient(create_app(settings=live_settings, llm=MockLLMClient())) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "url": "https://files.example.org/interview.wav",
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ]
            },
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert "失败 1 份" in content
        assert "interview.wav" in content


def test_live_document_can_use_real_parser_through_same_chat_endpoint() -> None:
    service = MaterialIngestService(
        downloader=MockDownloader(),
        asr=MockASRProvider(),
        ocr=MockOCRProvider(),
        document_parser=LocalDocumentParser(max_document_chars=1000),
        enabled_modalities=frozenset({MaterialModality.document}),
    )
    with TestClient(
        create_app(
            settings=replace(settings(), app_mode="live"),
            llm=MockLLMClient(),
            material_ingestor=service,
        )
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请进入质性材料分析"},
                            {
                                "type": "file",
                                "file": {
                                    "url": "https://files.example.org/material.txt",
                                    "filename": "material.txt",
                                },
                            },
                        ],
                    }
                ]
            },
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert "自动可用 1 份" in content
        assert "material.txt" in content


def test_multimodal_contract_rejects_data_url_and_unsupported_type() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        data_url = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "url": "data:audio/wav;base64,AAAA",
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ]
            },
        )
        assert data_url.status_code == 422

        video = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_video",
                                "input_video": {"url": "https://files.example.org/demo.mp4"},
                            }
                        ],
                    }
                ]
            },
        )
        assert video.status_code == 422
