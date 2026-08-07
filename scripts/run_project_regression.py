from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from app.config import PROJECT_ROOT, get_settings
from app.llm import MockLLMClient, OpenAICompatibleClient
from app.models import ProjectContext
from app.run_store import RunStore
from app.workflows import WRITEBACK_CONFIG, WorkflowService

FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"


async def main(live: bool) -> int:
    settings = get_settings()
    llm = OpenAICompatibleClient(settings) if live else MockLLMClient()
    mode = "live" if live else "mock"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir = PROJECT_ROOT / "test-results" / mode / f"v2.2.0-project-{stamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    all_cases = json.loads(
        (FIXTURE_ROOT / "regression_cases.json").read_text(encoding="utf-8")
    )
    selected = []
    for workflow_id in ("w1", "w2", "w3", "w4"):
        selected.append(next(case for case in all_cases if case["workflow_id"] == workflow_id))

    project_context = ProjectContext(
        project_id="P-regression",
        project_name="v1.3项目卡串联回归",
        revision=3,
        research_question="哪些条件影响社会实践参与和服务体验？",
        target_population="相关大学生与社区参与者",
        research_context="自动测试使用的虚构项目，不含真实个人信息。",
        method_plan="使用访谈与观察材料进行质性分析。",
    )
    results: list[dict[str, object]] = []
    failures = 0
    for case in selected:
        store = RunStore()
        service = WorkflowService(store, llm, settings)
        record = await store.create(case["workflow_id"])
        source_path = FIXTURE_ROOT / case["source_file"] if case.get("source_file") else None
        await service.execute(
            record.run_id,
            case["workflow_id"],
            case["fields"],
            source_path.name if source_path else None,
            source_path.read_bytes() if source_path else None,
            project_context,
        )
        finished = await store.get(record.run_id)
        assert finished is not None
        patch = finished.proposed_project_patch
        allowed = WRITEBACK_CONFIG[case["workflow_id"]]["allowed"]
        unauthorized = [
            update.path.value
            for update in (patch.updates if patch is not None else [])
            if update.path not in allowed
        ]
        passed = (
            finished.status == "succeeded"
            and patch is not None
            and patch.workflow_id.value == case["workflow_id"]
            and not unauthorized
        )
        if not passed:
            failures += 1
        result = {
            "case_id": case["case_id"],
            "workflow_id": case["workflow_id"],
            "passed": passed,
            "run_status": finished.status,
            "unauthorized_fields": unauthorized,
            "project_patch": patch.model_dump(mode="json") if patch else None,
            "final_markdown": finished.final_markdown,
        }
        results.append(result)
        print(f"[{'PASS' if passed else 'FAIL'}] {case['workflow_id']} {case['title']}")

    summary = {
        "version": "2.2.0",
        "mode": mode,
        "total": len(results),
        "passed": len(results) - failures,
        "failed": failures,
        "note": "Mock 只验证工程契约；Live 结果需要人工审查字段事实边界。",
    }
    (result_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (result_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{result_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行行小道 v2.2.0 项目卡写回回归")
    parser.add_argument(
        "--live",
        action="store_true",
        help="调用 .env 当前真实模型并产生 API 用量；默认使用无成本模拟模型",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
