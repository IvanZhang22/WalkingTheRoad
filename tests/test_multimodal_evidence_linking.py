from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel

from app.config import Settings
from app.evidence import verify_material_evidence
from app.multimodal.evidence_linking import prepare_w3_material_bundle
from app.multimodal.models import (
    Material,
    MaterialLocator,
    MaterialModality,
    MaterialSegment,
    MaterialStatus,
)
from app.run_store import RunStore
from app.workflows import WorkflowService

T = TypeVar("T", bound=BaseModel)


def material() -> Material:
    material_id = "MAT_AAAAAAAAAAAA"
    return Material(
        material_id=material_id,
        source_fingerprint="a" * 64,
        filename="访谈.mp3",
        modality=MaterialModality.audio,
        status=MaterialStatus.manual_review,
        normalized_text="自动可用原话。\n不得进入提示词的低置信原话。",
        automatic_text="自动可用原话。",
        provider_name="test",
        provider_model="test-asr",
        automatic_evidence_use=True,
        review_queue=["SEG_AAAAAAAAAAAA_0002"],
        segments=[
            MaterialSegment(
                segment_id="SEG_AAAAAAAAAAAA_0001",
                material_id=material_id,
                modality=MaterialModality.audio,
                text="自动可用原话。",
                provider_confidence=0.97,
                locator=MaterialLocator(start_ms=1000, end_ms=2500),
                automatic_evidence_use=True,
            ),
            MaterialSegment(
                segment_id="SEG_AAAAAAAAAAAA_0002",
                material_id=material_id,
                modality=MaterialModality.audio,
                text="不得进入提示词的低置信原话。",
                provider_confidence=0.42,
                locator=MaterialLocator(start_ms=2600, end_ms=4000),
                automatic_evidence_use=False,
                quality_flags=["low_confidence"],
            ),
        ],
    )


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
    )


def test_bundle_only_contains_automatic_segments() -> None:
    bundle = prepare_w3_material_bundle([material()], max_characters=1000)
    payload = json.loads(bundle.source_text)

    assert bundle.source_type == "单份访谈"
    assert bundle.character_count == len("自动可用原话。")
    assert list(bundle.segment_index) == ["SEG_AAAAAAAAAAAA_0001"]
    assert payload["material_segments"][0]["text"] == "自动可用原话。"
    assert "低置信" not in bundle.source_text


def test_segment_verification_rejects_cross_segment_or_fabricated_binding() -> None:
    bundle = prepare_w3_material_bundle([material()], max_characters=1000)
    data = {
        "evidence": [
            {
                "evidence_id": "EV_1",
                "source_id": "SEG_AAAAAAAAAAAA_0001",
                "quote": "自动可用原话。",
            },
            {
                "evidence_id": "EV_2",
                "source_id": "SEG_AAAAAAAAAAAA_9999",
                "quote": "自动可用原话。",
            },
        ]
    }
    verified, rejected = verify_material_evidence(data, bundle.source_text, bundle.segment_index)

    first = verified["evidence"][0]
    assert first["verification"] == "exact_match"
    assert first["source_segment_id"] == "SEG_AAAAAAAAAAAA_0001"
    assert first["material_id"] == "MAT_AAAAAAAAAAAA"
    assert first["location"] == {"start_ms": 1000, "end_ms": 2500}
    assert first["provider_confidence"] == 0.97
    assert verified["evidence"][1]["verification"] == "rejected"
    assert verified["evidence"][1]["quote"] == ""
    assert rejected[0]["reason"] == "source_segment_not_found_or_quote_not_exact"


class EvidenceAwareLLM:
    def __init__(self) -> None:
        self.prompts: dict[str, str] = {}

    async def complete(
        self,
        *,
        node_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        json_model: type[T] | None = None,
    ) -> str:
        del system_prompt, temperature, json_model
        self.prompts[node_id] = user_prompt
        if node_id == "3L-3-1":
            segment_id = re.search(r'"source_segment_id": "([^"]+)"', user_prompt).group(1)  # type: ignore[union-attr]
            return json.dumps(
                {
                    "material_summary": "一段访谈",
                    "source_ids": [segment_id],
                    "open_codes": [
                        {
                            "code_id": "C01",
                            "label": "入口障碍",
                            "meaning": "查找服务入口困难",
                            "type": "评价",
                        }
                    ],
                    "evidence": [
                        {
                            "evidence_id": "EV_1",
                            "source_id": segment_id,
                            "code_id": "C01",
                            "quote": "自动可用原话。",
                            "context": "当前片段",
                            "support_type": "直接支持",
                        }
                    ],
                    "contrasts": [],
                    "uncertainties": [],
                },
                ensure_ascii=False,
            )
        if node_id == "3L-3-2":
            return "# 多模态 W3 报告\n\n已核验引文：自动可用原话。"
        raise AssertionError(f"unexpected node: {node_id}")


async def test_workflow_runs_w3_with_segment_location_and_without_review_text() -> None:
    store = RunStore()
    llm = EvidenceAwareLLM()
    report = await WorkflowService(store, llm, settings()).analyze_materials(
        [material()], "哪些因素影响服务使用？"
    )

    assert report.startswith("# 多模态 W3 报告")
    assert "不得进入提示词" not in llm.prompts["3L-3-1"]
    assert '"start_ms": 1000' in llm.prompts["3L-3-2"]
    assert '"provider_confidence": 0.97' in llm.prompts["3L-3-2"]
