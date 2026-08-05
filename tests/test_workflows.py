from pathlib import Path

import pytest

from app.config import Settings
from app.llm import MockLLMClient
from app.models import RunStatus
from app.run_store import RunStore
from app.workflows import WorkflowService

FIXTURES = Path(__file__).parent / "fixtures" / "materials"


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


async def run_workflow(workflow_id: str, fields: dict, file_path: Path | None = None):
    store = RunStore()
    service = WorkflowService(store, MockLLMClient(), settings())
    record = await store.create(workflow_id)
    content = file_path.read_bytes() if file_path else None
    await service.execute(
        record.run_id,
        workflow_id,
        fields,
        file_path.name if file_path else None,
        content,
    )
    return await store.get(record.run_id)


@pytest.mark.asyncio
async def test_w1_full_node_chain() -> None:
    record = await run_workflow(
        "w1",
        {"theme": "社区服务", "purpose": "了解使用障碍"},
    )
    assert record is not None and record.status == RunStatus.succeeded
    assert [trace.legacy_node_id for trace in record.traces] == [
        "1I-1-1",
        "3L-1-1",
        "3L-1-2",
        "2O-1-1",
        "9E-1-1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "expected_nodes"),
    [
        (
            {
                "mode": "generate",
                "research_question": "为什么参与？",
                "participant_profile": "大学生",
                "duration": "30分钟",
            },
            [
                "2O-2-1",
                "1I-2-1",
                "3L-2-1",
                "3L-2-2",
                "2O-2-2",
                "9E-2-2",
            ],
        ),
        (
            {"mode": "review", "existing_questions": "大家是不是都满意？"},
            ["2O-2-1", "1I-2-2", "3L-2-2", "2O-2-2", "9E-2-2"],
        ),
    ],
)
async def test_w2_dual_intent_reuses_review(fields: dict, expected_nodes: list[str]) -> None:
    record = await run_workflow("w2", fields)
    assert record is not None and record.status == RunStatus.succeeded
    assert [trace.legacy_node_id for trace in record.traces] == expected_nodes


@pytest.mark.asyncio
async def test_w3_and_w4_complete_with_single_file() -> None:
    w3 = await run_workflow(
        "w3",
        {
            "research_question": "哪些条件影响使用？",
            "source_id": "PACK-A",
            "source_type": "混合材料",
            "source_context": "虚构测试",
        },
        FIXTURES / "w3-community-meal.txt",
    )
    assert w3 is not None and w3.status == RunStatus.succeeded
    assert "7C-3-1" in [trace.legacy_node_id for trace in w3.traces]

    w4 = await run_workflow(
        "w4",
        {
            "research_question": "服务使用",
            "candidate_claim": "C01：所有人都停止使用。",
            "target_population": "社区老人",
            "sample_summary": "3人",
            "source_id": "PACK-QA-A",
            "source_context": "虚构测试",
        },
        FIXTURES / "w4-community-meal.txt",
    )
    assert w4 is not None and w4.status == RunStatus.succeeded
    assert "7C-4-1" in [trace.legacy_node_id for trace in w4.traces]


@pytest.mark.asyncio
async def test_missing_required_file_fails_without_following_nodes() -> None:
    record = await run_workflow(
        "w3",
        {"research_question": "问题", "source_id": "PACK-X", "source_type": "单份访谈"},
    )
    assert record is not None and record.status == RunStatus.failed
    assert [trace.legacy_node_id for trace in record.traces] == ["1I-3-1"]
