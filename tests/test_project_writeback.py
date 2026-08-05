import json
from pathlib import Path

import pytest

from app.config import Settings
from app.llm import MockLLMClient
from app.models import ProjectContext, RunStatus
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


class UnauthorizedWritebackLLM:
    def __init__(self) -> None:
        self.mock = MockLLMClient()

    async def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["node_id"] == "3L-1-3":
            return json.dumps(
                {
                    "workflow_id": "w1",
                    "updates": [
                        {
                            "path": "audit_status",
                            "proposed_value": "越权写入",
                            "reason": "恶意测试",
                        }
                    ],
                    "stage_after_confirmation": "w1_confirmed",
                    "next_workflow": "w2",
                    "missing_prerequisites": [],
                    "warning": "",
                },
                ensure_ascii=False,
            )
        return await self.mock.complete(**kwargs)


@pytest.mark.asyncio
async def test_writeback_failure_uses_safe_fallback_without_failing_workflow() -> None:
    store = RunStore()
    service = WorkflowService(store, UnauthorizedWritebackLLM(), settings())
    record = await store.create("w1")
    await service.execute(
        record.run_id,
        "w1",
        {"theme": "社区服务", "purpose": "理解使用障碍"},
        None,
        None,
        ProjectContext(project_id="P-test", project_name="测试项目"),
    )
    finished = await store.get(record.run_id)
    assert finished is not None and finished.status == RunStatus.succeeded
    assert finished.proposed_project_patch is not None
    assert finished.proposed_project_patch.warning.startswith("项目卡结构化建议生成失败")
    assert all(
        update.path != "audit_status" for update in finished.proposed_project_patch.updates
    )
    writeback_trace = next(
        trace for trace in finished.traces if trace.legacy_node_id == "3L-1-3"
    )
    assert writeback_trace.status == "failed"


def test_project_context_rejects_raw_material_and_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ProjectContext.model_validate(
            {
                "project_id": "P-1",
                "project_name": "测试",
                "source_text": "不应进入后端项目上下文",
            }
        )


@pytest.mark.asyncio
async def test_w3_material_writeback_uses_server_metadata_without_source_text() -> None:
    file_path = FIXTURES / "w3-community-meal.txt"
    file_bytes = file_path.read_bytes()
    store = RunStore()
    service = WorkflowService(store, MockLLMClient(), settings())
    record = await store.create("w3")
    await service.execute(
        record.run_id,
        "w3",
        {
            "research_question": "哪些条件影响使用？",
            "source_id": "PACK-A",
            "source_type": "混合材料",
            "source_context": "虚构测试",
        },
        file_path.name,
        file_bytes,
        ProjectContext(project_id="P-test", project_name="测试项目"),
    )
    finished = await store.get(record.run_id)
    assert finished is not None and finished.proposed_project_patch is not None
    material_update = next(
        update
        for update in finished.proposed_project_patch.updates
        if update.path == "materials"
    )
    dumped = material_update.model_dump(mode="json")
    material = dumped["proposed_value"][0]
    assert material["source_id"] == "PACK-A"
    assert material["size_bytes"] == len(file_bytes)
    assert len(material["sha256"]) == 64
    assert "source_text" not in material
