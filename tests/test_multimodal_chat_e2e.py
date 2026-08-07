from __future__ import annotations

import json
import re
from typing import TypeVar

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.config import Settings
from app.llm import MockLLMClient
from app.main import create_app
from app.multimodal.downloader import MockDownloader
from app.multimodal.providers.deepgram_asr import DeepgramASRProvider
from app.multimodal.providers.mock import MockDocumentParser, MockOCRProvider
from app.multimodal.service import MaterialIngestService

T = TypeVar("T", bound=BaseModel)


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


class FullLoopLLM(MockLLMClient):
    def __init__(self) -> None:
        self.saw_verified_location = False

    async def complete(
        self,
        *,
        node_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        json_model: type[T] | None = None,
    ) -> str:
        if node_id == "3L-3-1":
            match = re.search(r'"source_segment_id": "([^"]+)"', user_prompt)
            assert match is not None
            return json.dumps(
                {
                    "material_summary": "学生认为就业服务入口不易找到。",
                    "source_ids": [match.group(1)],
                    "open_codes": [
                        {
                            "code_id": "C01",
                            "label": "入口难找",
                            "meaning": "服务入口可达性不足",
                            "type": "评价",
                        }
                    ],
                    "evidence": [
                        {
                            "evidence_id": "EV_001",
                            "source_id": match.group(1),
                            "code_id": "C01",
                            "quote": "我找了很久才找到就业服务入口。",
                            "context": "学生描述使用过程",
                            "support_type": "直接支持",
                        }
                    ],
                    "contrasts": [],
                    "uncertainties": ["仅有一份材料"],
                },
                ensure_ascii=False,
            )
        if node_id == "3L-3-2":
            self.saw_verified_location = (
                '"verification": "exact_match"' in user_prompt
                and '"start_ms": 500' in user_prompt
                and '"end_ms": 2800' in user_prompt
                and '"provider_confidence": 0.96' in user_prompt
            )
            return (
                "# 质性材料分析报告\n\n"
                "当前材料中的候选发现：学生表示“我找了很久才找到就业服务入口。”"
            )
        return await super().complete(
            node_id=node_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            json_model=json_model,
        )


def test_audio_url_runs_real_provider_shape_through_verified_w3_in_same_endpoint() -> None:
    signed_url = "https://files.example.org/interview.mp3?signature=must-not-echo"

    def deepgram(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"url": signed_url}
        return httpx.Response(
            200,
            json={
                "metadata": {"model_info": {"id": {"name": "nova-3"}}},
                "results": {
                    "utterances": [
                        {
                            "start": 0.5,
                            "end": 2.8,
                            "confidence": 0.96,
                            "transcript": "我找了很久才找到就业服务入口。",
                            "speaker": 0,
                            "words": [],
                        }
                    ]
                },
            },
        )

    ingest = MaterialIngestService(
        downloader=MockDownloader(),
        asr=DeepgramASRProvider(api_key="test-key", transport=httpx.MockTransport(deepgram)),
        ocr=MockOCRProvider(),
        document_parser=MockDocumentParser(),
    )
    llm = FullLoopLLM()
    app = create_app(settings=settings(), llm=llm, material_ingestor=ingest)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-agent-key"},
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "研究问题：学生如何理解就业服务入口的可达性？请做主题分析。",
                            },
                            {
                                "type": "input_audio",
                                "input_audio": {"url": signed_url, "format": "mp3"},
                            },
                        ],
                    }
                ]
            },
        )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "已进入 **W3 质性材料分析**" in content
    assert "自动可用 1 份" in content
    assert "# 质性材料分析报告" in content
    assert "我找了很久才找到就业服务入口。" in content
    assert "must-not-echo" not in content
    assert signed_url not in content
    assert llm.saw_verified_location is True
