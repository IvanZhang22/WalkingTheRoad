"""Vercel Blob 浏览器直传授权与临时对象清理。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

UPLOAD_PREFIX = "xingxiaodao-uploads/"
TOKEN_TTL_SECONDS = 5 * 60
BLOB_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "12"
UPLOAD_NAME_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}-.+\.[A-Za-z0-9]{2,5}$")

ALLOWED_CONTENT_TYPES: dict[str, tuple[str, ...]] = {
    "mp3": ("audio/mpeg", "audio/mp3", "application/octet-stream"),
    "wav": ("audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"),
    "m4a": ("audio/mp4", "audio/x-m4a", "application/octet-stream"),
    "webm": ("audio/webm", "video/webm", "application/octet-stream"),
    "png": ("image/png", "application/octet-stream"),
    "jpg": ("image/jpeg", "application/octet-stream"),
    "jpeg": ("image/jpeg", "application/octet-stream"),
    "webp": ("image/webp", "application/octet-stream"),
    "pdf": ("application/pdf", "application/octet-stream"),
    "txt": ("text/plain", "application/octet-stream"),
    "md": ("text/markdown", "text/plain", "application/octet-stream"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    ),
}


class BlobGenerateTokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pathname: str = Field(min_length=1, max_length=500)
    clientPayload: str | None = Field(default=None, max_length=2_000)
    multipart: bool = False


class BlobGenerateTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["blob.generate-client-token"]
    payload: BlobGenerateTokenPayload


class BlobClientPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filename: str = Field(min_length=1, max_length=300)
    sizeBytes: int = Field(gt=0)
    contentType: str = Field(default="application/octet-stream", max_length=200)


def create_client_upload_token(
    request: BlobGenerateTokenRequest,
    *,
    read_write_token: str,
    max_bytes: int,
    now: float | None = None,
) -> dict[str, str]:
    """生成与 `@vercel/blob/client` 兼容的最小权限短期令牌。"""

    token = read_write_token.strip()
    store_id = _store_id(token)
    if max_bytes <= 0:
        raise ValueError("大文件上传限制必须大于 0。")

    upload = _client_payload(request.payload.clientPayload)
    pathname = _validate_pathname(request.payload.pathname, upload.filename)
    suffix = PurePosixPath(pathname).suffix.lower().lstrip(".")
    allowed_types = ALLOWED_CONTENT_TYPES[suffix]
    content_type = upload.contentType.lower() or "application/octet-stream"
    if content_type not in allowed_types:
        raise ValueError(f"文件 Content-Type 与扩展名 .{suffix} 不一致。")
    if upload.sizeBytes > max_bytes:
        raise ValueError(f"文件超过 {max_bytes // 1024 // 1024} MB 限制。")
    if request.payload.multipart and upload.sizeBytes <= 100 * 1024 * 1024:
        raise ValueError("100 MB 以内无需启用分片上传。")

    issued_at = time.time() if now is None else now
    token_payload: dict[str, Any] = {
        "pathname": pathname,
        "allowedContentTypes": list(allowed_types),
        "maximumSizeInBytes": max_bytes,
        "validUntil": round((issued_at + TOKEN_TTL_SECONDS) * 1000),
        "addRandomSuffix": True,
        "allowOverwrite": False,
        "cacheControlMaxAge": 60,
    }
    encoded_payload = base64.b64encode(
        json.dumps(token_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(
        token.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    secured = base64.b64encode(f"{signature}.{encoded_payload}".encode("ascii")).decode("ascii")
    client_token = f"vercel_blob_client_{store_id}_{secured}"
    return {"type": request.type, "clientToken": client_token}


def validate_managed_blob_url(url: str, *, filename: str) -> str:
    """只接受本应用前缀下的 Vercel 公有 Blob URL。"""

    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(".public.blob.vercel-storage.com"):
        raise ValueError("大文件地址不是受支持的 Vercel Blob 公网地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("大文件地址包含不允许的认证信息或查询参数。")
    pathname = parsed.path.lstrip("/")
    if not pathname.startswith(UPLOAD_PREFIX) or ".." in PurePosixPath(pathname).parts:
        raise ValueError("大文件地址不属于行小道临时上传目录。")
    expected_suffix = PurePosixPath(_safe_filename(filename)).suffix.lower()
    actual_suffix = PurePosixPath(pathname).suffix.lower()
    if not expected_suffix or expected_suffix != actual_suffix:
        raise ValueError("大文件地址扩展名与原文件名不一致。")
    if actual_suffix.lstrip(".") not in ALLOWED_CONTENT_TYPES:
        raise ValueError("大文件类型不受支持。")
    return url.strip()


async def delete_blob(
    url: str,
    *,
    read_write_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """删除已经处理完毕的临时公有 Blob；错误信息不回显 URL 或令牌。"""

    token = read_write_token.strip()
    store_id = _store_id(token)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-api-version": BLOB_API_VERSION,
        "x-api-blob-request-id": f"{store_id}:{int(time.time() * 1000)}:{uuid4().hex[:12]}",
        "x-api-blob-request-attempt": "0",
        "x-vercel-blob-store-id": store_id,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30),
            transport=transport,
            trust_env=False,
            headers=headers,
        ) as client:
            response = await client.post(f"{BLOB_API_URL}/delete", json={"urls": [url]})
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise RuntimeError("临时上传文件清理请求失败。") from exc
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"临时上传文件清理返回 HTTP {response.status_code}。")


def _store_id(token: str) -> str:
    parts = token.split("_", 4)
    if len(parts) < 5 or parts[:3] != ["vercel", "blob", "rw"] or not parts[3]:
        raise ValueError("BLOB_READ_WRITE_TOKEN 格式无效。")
    return parts[3]


def _client_payload(raw: str | None) -> BlobClientPayload:
    if not raw:
        raise ValueError("大文件上传缺少文件元数据。")
    try:
        return BlobClientPayload.model_validate_json(raw)
    except ValueError as exc:
        raise ValueError("大文件上传元数据无效。") from exc


def _validate_pathname(pathname: str, filename: str) -> str:
    normalized = pathname.strip().replace("\\", "/")
    if not normalized.startswith(UPLOAD_PREFIX):
        raise ValueError("大文件上传目录无效。")
    relative = normalized[len(UPLOAD_PREFIX) :]
    if (
        not relative
        or "/" in relative
        or ".." in PurePosixPath(normalized).parts
        or UPLOAD_NAME_PATTERN.fullmatch(relative) is None
    ):
        raise ValueError("大文件上传路径无效。")
    safe_filename = _safe_filename(filename)
    expected_suffix = PurePosixPath(safe_filename).suffix.lower()
    actual_suffix = PurePosixPath(relative).suffix.lower()
    if not expected_suffix or expected_suffix != actual_suffix:
        raise ValueError("上传路径扩展名与原文件名不一致。")
    if actual_suffix.lstrip(".") not in ALLOWED_CONTENT_TYPES:
        raise ValueError("当前文件类型不支持大文件上传。")
    return normalized


def _safe_filename(filename: str) -> str:
    normalized = filename.strip().replace("\\", "/")
    if (
        not normalized
        or "\x00" in normalized
        or PurePosixPath(normalized).name != normalized
        or normalized in {".", ".."}
    ):
        raise ValueError("上传文件名无效。")
    return normalized
