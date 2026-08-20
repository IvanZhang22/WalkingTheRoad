from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.knowledge import MethodologyKnowledgeBase
from app.llm import MockLLMClient
from app.main import create_app
from app.run_store import RunStore
from app.workflows import WorkflowService
from tests.test_openai_compat import headers, settings


def _knowledge_base() -> MethodologyKnowledgeBase:
    return MethodologyKnowledgeBase.from_directory(
        Path(__file__).resolve().parents[1] / "data" / "methodology_knowledge"
    )


def test_curated_knowledge_is_advisory_and_keeps_all_rule_cards() -> None:
    knowledge = _knowledge_base()
    status = knowledge.status()

    assert status["card_count"] == 36
    assert status["formal_rule_count"] == 0
    assert status["advisory_only"] is True
    hits = knowledge.retrieve("怎样设计不诱导受访者的访谈问题", "w2")
    assert any(hit.card.card_id == "W2-R02" for hit in hits)
    assert all(hit.card.workflow in {"global", "w2"} for hit in hits)


def test_source_view_labels_candidate_status_without_local_paths() -> None:
    source_view = _knowledge_base().source_markdown("怎么审计一个结论是否越界", "w4")

    assert "候选方法依据" in source_view
    assert "candidate_v2" in source_view
    assert "E:\\" not in source_view
    assert "原文完整版" not in source_view


def test_qingxiaoda_can_show_sources_only_on_user_request() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        session_id = "knowledge-source-view"
        first = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "sessionId": session_id,
                "messages": [{"role": "user", "content": "怎样设计不诱导受访者的访谈问题？"}],
            },
        )
        assert first.status_code == 200
        assert "查看依据" in first.json()["choices"][0]["message"]["content"]

        sources = client.post(
            "/v1/chat/completions",
            headers=headers(),
            json={
                "sessionId": session_id,
                "messages": [{"role": "user", "content": "查看依据"}],
            },
        )
        assert sources.status_code == 200
        output = sources.json()["choices"][0]["message"]["content"]
        assert "W2-R02" in output
        assert "候选方法依据" in output


async def test_w3_never_receives_case_or_methodology_context(tmp_path: Path) -> None:
    service = WorkflowService(
        RunStore(),
        MockLLMClient(),
        settings(),
        knowledge_base=_knowledge_base(),
    )
    record = await service.store.create("w3")
    await service.execute(
        record.run_id,
        "w3",
        {
            "research_question": "实践学生如何理解服务对象的需求？",
            "source_id": "PACK-1",
            "source_type": "单份访谈",
            "source_context": "模拟材料",
        },
        "material.md",
        "I01：我觉得先听对方的困难，再讨论如何帮助。".encode(),
    )
    run = await service.store.get(record.run_id)
    assert run is not None
    prompts = "\n".join(node.system_prompt for node in run.traces)
    assert "methodology_reference" not in prompts
    assert "CASE-W3-01" not in prompts
