from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "test-results",
    "dist",
    "build",
}
EXCLUDED_NAMES = {".env", "Thumbs.db", ".DS_Store"}


def find_git() -> str | None:
    candidates = [
        os.getenv("GIT_EXE"),
        shutil.which("git"),
        r"D:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def release_files() -> list[Path]:
    git = find_git()
    if git and (PROJECT_ROOT / ".git").exists():
        result = subprocess.run(
            [git, "-c", "core.quotepath=false", "-C", str(PROJECT_ROOT), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return [PROJECT_ROOT / line for line in result.stdout.splitlines() if line]
    return [
        path
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file()
        and path.name not in EXCLUDED_NAMES
        and not any(part in EXCLUDED_PARTS for part in path.parts)
    ]


def build(output_dir: Path) -> Path:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"xingxiaodao-agent-v{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(release_files()):
            relative = path.relative_to(PROJECT_ROOT)
            if path.name in EXCLUDED_NAMES or any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            handle.write(path, Path(f"xingxiaodao-agent-v{version}") / relative)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成不含本地密钥和运行数据的源码发布包")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist")
    args = parser.parse_args()
    result = build(args.output.resolve())
    print(result)
