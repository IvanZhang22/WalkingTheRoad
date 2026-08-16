from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    completed = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(204)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        type(self).code = query.get("code", [None])[0]
        type(self).error = query.get("error", [None])[0]
        ok = bool(type(self).code) and not type(self).error
        title = "OpenRouter 授权完成" if ok else "OpenRouter 授权失败"
        detail = "可以关闭此页面并返回 Codex。" if ok else "请关闭页面后重新运行授权。"
        body = (
            "<!doctype html><meta charset='utf-8'>"
            f"<title>{title}</title>"
            "<style>body{font-family:system-ui;margin:64px;line-height:1.7}</style>"
            f"<h1>{title}</h1><p>{detail}</p>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).completed.set()

    def log_message(self, format: str, *args: object) -> None:
        return


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _exchange_code(code: str, verifier: str) -> str:
    payload = json.dumps(
        {
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_EXCHANGE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenRouter 授权码交换失败（HTTP {exc.code}）") from exc
    key = result.get("key", "")
    if not isinstance(key, str) or not key.startswith("sk-or-"):
        raise RuntimeError("OpenRouter 未返回有效 API Key")
    return key


def _run_vercel(repo: Path, arguments: list[str], stdin: str | None = None) -> None:
    vercel = shutil.which("vercel") or shutil.which("vercel.cmd")
    if not vercel:
        raise RuntimeError("未找到 Vercel CLI")
    result = subprocess.run(
        [vercel, *arguments],
        cwd=repo,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Vercel 环境变量写入失败：{message}")


def _configure_vercel(repo: Path, key: str) -> None:
    _run_vercel(
        repo,
        [
            "env",
            "add",
            "OPENROUTER_API_KEY",
            "production,preview",
            "--sensitive",
            "--force",
            "--yes",
        ],
        stdin=f"{key}\n",
    )
    variables = {
        "MODEL_PROVIDER": "openrouter",
        "MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "MODEL_NAME": "openrouter/free",
    }
    for name, value in variables.items():
        _run_vercel(
            repo,
            [
                "env",
                "add",
                name,
                "production,preview",
                "--no-sensitive",
                "--force",
                "--yes",
                "--value",
                value,
            ],
        )


def _smoke_openrouter(key: str) -> str:
    payload = json.dumps(
        {
            "model": "openrouter/free",
            "messages": [
                {
                    "role": "user",
                    "content": "Return JSON with ok=true. Do not include other fields.",
                }
            ],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "xingxiaodao_live_smoke",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean", "const": True}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                },
            },
        }
    ).encode()
    request = urllib.request.Request(
        OPENROUTER_COMPLETIONS_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenRouter 真实模型测试失败（HTTP {exc.code}）") from exc
    try:
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        model = str(result.get("model", "unknown"))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenRouter 真实模型测试返回格式无效") from exc
    if parsed != {"ok": True}:
        raise RuntimeError("OpenRouter 真实模型测试未通过结构化 JSON 校验")
    return model


def _smoke_project_client(key: str) -> None:
    from app.config import Settings
    from app.llm import OpenAICompatibleClient
    from app.models import ResearchDiagnosis

    async def run() -> None:
        settings = Settings(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            model="openrouter/free",
            thinking="disabled",
            app_mode="live",
            timeout_seconds=120,
            max_upload_bytes=20 * 1024 * 1024,
            max_document_chars=300_000,
            provider="openrouter",
        )
        client = OpenAICompatibleClient(settings)
        result = await client.complete(
            node_id="live-smoke",
            system_prompt="你是 JSON 接口测试助手，只按给定 Schema 输出。",
            user_prompt="返回研究诊断对象，所有数组字段均为空数组。",
            temperature=0,
            json_model=ResearchDiagnosis,
        )
        parsed = json.loads(result)
        if set(parsed) != set(ResearchDiagnosis.model_fields):
            raise RuntimeError("项目模型客户端未返回完整 ResearchDiagnosis Schema")
        if not all(value == [] for value in parsed.values()):
            raise RuntimeError("项目模型客户端结构化 JSON 内容不符合测试要求")

    asyncio.run(run())


def _open_browser(url: str) -> None:
    candidates = [
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    edge = next((candidate for candidate in candidates if candidate.is_file()), None)
    if edge:
        subprocess.Popen(
            [str(edge), url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if not webbrowser.open(url, new=2):
        raise RuntimeError("无法自动打开浏览器")


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 OpenRouter OAuth 配置 Vercel")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    verifier, challenge = _pkce_pair()
    OAuthCallbackHandler.code = None
    OAuthCallbackHandler.error = None
    OAuthCallbackHandler.completed.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    callback_url = f"http://localhost:{server.server_port}/callback"
    auth_url = f"{OPENROUTER_AUTH_URL}?{urllib.parse.urlencode({'callback_url': callback_url, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print("浏览器授权页已打开；请登录并点击 Authorize。", flush=True)
    _open_browser(auth_url)
    completed = OAuthCallbackHandler.completed.wait(args.timeout)
    server.shutdown()
    server.server_close()
    if not completed:
        raise RuntimeError("等待 OpenRouter 授权超时")
    if OAuthCallbackHandler.error or not OAuthCallbackHandler.code:
        raise RuntimeError(f"OpenRouter 授权失败：{OAuthCallbackHandler.error or '缺少授权码'}")

    key = _exchange_code(OAuthCallbackHandler.code, verifier)
    _configure_vercel(args.repo.resolve(), key)
    print("OpenRouter 已授权，密钥已安全写入 Vercel Production 和 Preview。", flush=True)
    routed_model = _smoke_openrouter(key)
    print(f"OpenRouter 真实模型测试通过（路由模型：{routed_model}）。", flush=True)
    _smoke_project_client(key)
    print("项目 OpenAICompatibleClient 结构化 JSON 测试通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
