from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(label: str, command: list[str]) -> None:
    print(f"\n[QUALITY] {label}")
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> None:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 Node.js；项目卡测试和前端语法检查无法运行。")
    python = sys.executable
    run("仓库安全", [python, "scripts/check_repository_safety.py"])
    run("Ruff", [python, "-m", "ruff", "check", "."])
    run("Mypy", [python, "-m", "mypy", "app"])
    run("Pytest", [python, "-m", "pytest"])
    run("项目卡 Node 测试", [node, "--test", "tests/js/test_project_store.cjs"])
    run("前端主脚本语法", [node, "--check", "app/static/app.js"])
    run("项目卡脚本语法", [node, "--check", "app/static/project-store.js"])
    run("四工作流回归", [python, "scripts/run_regression.py"])
    run("意图路由回归", [python, "scripts/run_routing_regression.py"])
    run("项目卡写回回归", [python, "scripts/run_project_regression.py"])
    print("\n全部质量门通过。")


if __name__ == "__main__":
    main()
