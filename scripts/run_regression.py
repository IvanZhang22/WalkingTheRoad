from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from app.config import PROJECT_ROOT, get_settings
from app.llm import MockLLMClient, OpenAICompatibleClient
from app.run_store import RunStore
from app.workflows import WorkflowService

FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"


async def main(live: bool) -> int:
    settings = get_settings()
    llm = OpenAICompatibleClient(settings) if live else MockLLMClient()
    mode = "live" if live else "mock"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir = PROJECT_ROOT / "test-results" / mode / f"v2.0.0-{stamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    cases = json.loads((FIXTURE_ROOT / "regression_cases.json").read_text(encoding="utf-8"))

    score_rows: list[dict[str, str]] = []
    failures = 0
    for case in cases:
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
        )
        finished = await store.get(record.run_id)
        assert finished is not None
        payload = {
            "case_id": case["case_id"],
            "title": case["title"],
            "input": case,
            "run": finished.model_dump(mode="json"),
        }
        (result_dir / f"{case['case_id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if finished.final_markdown:
            (result_dir / f"{case['case_id']}.md").write_text(
                finished.final_markdown, encoding="utf-8"
            )
        if finished.status == "failed":
            failures += 1
        score_rows.append(
            {
                "case_id": case["case_id"],
                "title": case["title"],
                "run_status": finished.status,
                "事实与边界_0至5": "",
                "方法适切性_0至5": "",
                "证据可追溯_0至5": "",
                "结构与可执行性_0至5": "",
                "总评与修改意见": "",
            }
        )
        print(f"[{finished.status}] {case['case_id']} {case['title']}")

    with (result_dir / "人工评分表.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=score_rows[0].keys())
        writer.writeheader()
        writer.writerows(score_rows)
    print(f"结果目录：{result_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行行小道 v2.0.0 的既有四工作流回归测试")
    parser.add_argument(
        "--live",
        action="store_true",
        help="调用 .env 中当前启用的真实模型；不加时使用无成本模拟模型",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
