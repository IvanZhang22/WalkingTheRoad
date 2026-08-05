from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_IGNORES = {".env", ".venv/", "test-results/", "dist/"}
FORBIDDEN_TRACKED_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_TRACKED_SUFFIXES = {
    ".docx",
    ".xlsx",
    ".xls",
    ".wav",
    ".mp3",
    ".m4a",
    ".mp4",
}
EXCLUDED_DIRS = {
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
SECRET_PATTERNS = {
    "OpenAI/模型风格密钥": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub Token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "私钥": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Bearer Token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{30,}", re.IGNORECASE),
}
MAX_TRACKED_BYTES = 5 * 1024 * 1024


def find_git() -> str | None:
    configured = os.getenv("GIT_EXE")
    candidates = [
        configured,
        shutil.which("git"),
        r"D:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def tracked_files() -> list[Path]:
    git = find_git()
    if git and (PROJECT_ROOT / ".git").exists():
        result = subprocess.run(
            [
                git,
                "-c",
                "core.quotepath=false",
                "-C",
                str(PROJECT_ROOT),
                "ls-files",
                "-co",
                "--exclude-standard",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return [PROJECT_ROOT / line for line in result.stdout.splitlines() if line]
    return [
        path
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file() and not any(part in EXCLUDED_DIRS for part in path.parts)
        and path.name != ".env"
    ]


def scan() -> list[str]:
    errors: list[str] = []
    ignore_path = PROJECT_ROOT / ".gitignore"
    ignore_lines = {
        line.strip()
        for line in ignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing_ignores = sorted(REQUIRED_IGNORES - ignore_lines)
    if missing_ignores:
        errors.append(f".gitignore 缺少：{', '.join(missing_ignores)}")

    for path in tracked_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if path.name.lower() in FORBIDDEN_TRACKED_NAMES:
            errors.append(f"禁止入库的敏感文件：{relative}")
            continue
        if path.suffix.lower() in FORBIDDEN_TRACKED_SUFFIXES:
            errors.append(f"禁止入库的二进制/原始材料：{relative}")
        if path.stat().st_size > MAX_TRACKED_BYTES:
            errors.append(f"文件超过 5MB：{relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"疑似{label}：{relative}")
    return errors


if __name__ == "__main__":
    problems = scan()
    if problems:
        print("仓库安全检查失败：")
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)
    print("仓库安全检查通过：未发现被跟踪的密钥、私钥、真实办公附件或超大文件。")
