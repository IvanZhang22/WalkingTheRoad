from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from app.config import PROJECT_ROOT, get_settings
from app.llm import MockLLMClient, OpenAICompatibleClient
from app.routing import IntentRouter

CASES_PATH = PROJECT_ROOT / "tests" / "fixtures" / "intent_routing_cases.json"


async def main(live: bool) -> int:
    settings = get_settings()
    llm = OpenAICompatibleClient(settings) if live else MockLLMClient()
    mode = "live" if live else "mock"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir = PROJECT_ROOT / "test-results" / mode / f"v2.0.0-routing-{stamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []

    for case in cases:
        error = ""
        try:
            result = await IntentRouter(llm).route(case["message"])
            actual = (
                result.recommended_workflow.value
                if hasattr(result.recommended_workflow, "value")
                else str(result.recommended_workflow)
            )
            secondary = (
                result.possible_secondary_workflow.value
                if result.possible_secondary_workflow is not None
                else ""
            )
            reason = result.reason
            confidence = result.confidence.value
        except Exception as exc:
            actual, secondary, reason, confidence = "error", "", "", ""
            error = str(exc)
        primary_pass = actual == case["expected"]
        expected_secondary = case.get("expected_secondary", "")
        secondary_pass = not expected_secondary or secondary == expected_secondary
        passed = primary_pass and secondary_pass and not error
        row = {
            "case_id": case["case_id"],
            "category": case["category"],
            "message": case["message"],
            "expected": case["expected"],
            "actual": actual,
            "expected_secondary": expected_secondary,
            "actual_secondary": secondary,
            "confidence": confidence,
            "reason": reason,
            "passed": passed,
            "error": error,
        }
        rows.append(row)
        print(f"[{'PASS' if passed else 'FAIL'}] {case['case_id']} {actual}")

    passed_count = sum(bool(row["passed"]) for row in rows)
    category_stats = {
        category: {
            "total": sum(row["category"] == category for row in rows),
            "passed": sum(row["category"] == category and bool(row["passed"]) for row in rows),
        }
        for category in sorted({str(row["category"]) for row in rows})
    }
    summary = {
        "version": "2.0.0",
        "mode": mode,
        "total": len(rows),
        "passed": passed_count,
        "failed": len(rows) - passed_count,
        "actual_distribution": dict(Counter(str(row["actual"]) for row in rows)),
        "category_stats": category_stats,
        "note": "mock 只验证工程分支；live 结果才可用于评价模型路由质量。",
    }
    (result_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (result_dir / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (result_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{result_dir}")
    return 0 if passed_count == len(rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行行小道 v2.0.0 意图路由回归测试")
    parser.add_argument(
        "--live",
        action="store_true",
        help="调用 .env 中当前启用的真实模型并产生 API 用量；默认使用无成本模拟模型",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
