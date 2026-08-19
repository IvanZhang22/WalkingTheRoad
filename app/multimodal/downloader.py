"""异步安全下载：SSRF、重定向、大小、类型和临时文件防护。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import os
import socket
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit
from zipfile import BadZipFile, ZipFile

import httpx

from app.multimodal.contracts import (
    AUDIO_FORMATS,
    FileContentPart,
    ImageUrlContentPart,
    InputAudioContentPart,
)
from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile

AttachmentPart = InputAudioContentPart | ImageUrlContentPart | FileContentPart

SUPPORTED_MIME: dict[str, frozenset[str]] = {
    "mp3": frozenset({"audio/mpeg", "audio/mp3"}),
    "wav": frozenset({"audio/wav", "audio/x-wav", "audio/wave"}),
    "m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    "webm": frozenset({"audio/webm", "video/webm"}),
    "png": frozenset({"image/png"}),
    "jpg": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "webp": frozenset({"image/webp"}),
    "pdf": frozenset({"application/pdf"}),
    "txt": frozenset({"text/plain"}),
    "md": frozenset({"text/markdown", "text/plain"}),
    "docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
        }
    ),
    "xlsx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        }
    ),
}
PREFERRED_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
GENERIC_MIME = frozenset({"", "application/octet-stream", "binary/octet-stream"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal"})


class Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> list[str] | Awaitable[list[str]]: ...


class MaterialDownloader(ABC):
    @abstractmethod
    async def download(self, source: AttachmentPart) -> DownloadedFile:
        """下载为临时文件；调用方负责在 finally 中删除 path。"""

    async def validate_source_url(self, source: AttachmentPart) -> None:
        """仅 URL 直送上游 Provider 时使用；默认实现拒绝该能力。"""

        raise MaterialIngestError("XDW-HTTP-URL", "当前下载器不支持 URL 直送识别服务。")


async def system_resolver(hostname: str, port: int) -> list[str]:
    def resolve() -> list[str]:
        rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return sorted({str(row[4][0]).split("%", 1)[0] for row in rows})

    try:
        return await asyncio.to_thread(resolve)
    except OSError as exc:
        raise MaterialIngestError(
            "XDW-HTTP-DNS",
            "附件域名解析失败，请确认地址可公开访问。",
            retryable=True,
        ) from exc


class SafeDownloader(MaterialDownloader):
    def __init__(
        self,
        *,
        max_bytes: int,
        connect_timeout: float = 10,
        read_timeout: float = 120,
        max_redirects: int = 3,
        retries: int = 1,
        tmp_dir: str | Path | None = None,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        trusted_public_hosts: frozenset[str] = frozenset(),
    ) -> None:
        if max_bytes <= 0 or connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("下载大小和超时必须大于 0")
        if max_redirects < 0 or retries < 0:
            raise ValueError("重定向和重试次数不得为负数")
        self.max_bytes = max_bytes
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_redirects = max_redirects
        self.retries = retries
        self.tmp_dir = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.gettempdir())
        self.resolver = resolver
        self.transport = transport
        self.trusted_public_hosts = frozenset(host.lower() for host in trusted_public_hosts)

    async def download(self, source: AttachmentPart) -> DownloadedFile:
        url, filename, suffix = _source_fields(source)
        last_error: MaterialIngestError | None = None
        for attempt in range(self.retries + 1):
            try:
                async with asyncio.timeout(self.connect_timeout + self.read_timeout):
                    return await self._download_once(url, filename, suffix)
            except MaterialIngestError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.retries:
                    raise
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = MaterialIngestError(
                    "XDW-HTTP-TRANSPORT",
                    "附件下载超时或网络不可用，请稍后重试。",
                    retryable=True,
                )
                if attempt >= self.retries:
                    raise last_error from exc
        assert last_error is not None
        raise last_error

    async def validate_source_url(self, source: AttachmentPart) -> None:
        """在调用支持 URL 的 Provider 前执行与下载路径相同的 SSRF 校验。"""

        url, _, _ = _source_fields(source)
        await self.validate_url(url)

    async def validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise MaterialIngestError("XDW-HTTP-URL", "附件地址必须使用 http 或 https。")
        if parsed.username or parsed.password:
            raise MaterialIngestError("XDW-HTTP-URL", "附件地址不得包含用户名或密码。")
        host = parsed.hostname.rstrip(".").lower()
        if host in BLOCKED_HOSTS or host.endswith((".localhost", ".internal")):
            raise MaterialIngestError("XDW-HTTP-SSRF", "附件地址指向了禁止访问的主机。")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise MaterialIngestError("XDW-HTTP-URL", "附件地址端口无效。") from exc

        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            resolved = self.resolver(host, port)
            addresses = await resolved if inspect.isawaitable(resolved) else resolved
        else:
            addresses = [str(literal)]
        if not addresses or any(not _is_public_ip(value) for value in addresses):
            raise MaterialIngestError("XDW-HTTP-SSRF", "附件地址解析到了禁止访问的 IP。")

    async def _download_once(self, url: str, filename: str, suffix: str) -> DownloadedFile:
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        current = url
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
            headers={"User-Agent": "Xingxiaodao-Agent/2.2"},
        ) as client:
            for redirect_count in range(self.max_redirects + 1):
                await self.validate_url(current)
                async with client.stream("GET", current) as response:
                    _validate_connected_peer(response)
                    if response.status_code in REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location or redirect_count >= self.max_redirects:
                            raise MaterialIngestError(
                                "XDW-HTTP-REDIRECT",
                                "附件地址重定向次数过多或缺少目标地址。",
                            )
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise MaterialIngestError(
                            "XDW-HTTP-STATUS",
                            f"附件服务返回 HTTP {response.status_code}。",
                            retryable=response.status_code >= 500,
                        )
                    host = (urlsplit(url).hostname or "").lower()
                    trusted_source_url = (
                        url
                        if suffix in AUDIO_FORMATS
                        and current == url
                        and any(
                            host == trusted
                            or (trusted.startswith("*.") and host.endswith(trusted[1:]))
                            for trusted in self.trusted_public_hosts
                        )
                        else None
                    )
                    return await self._stream_to_temp(
                        response,
                        filename,
                        suffix,
                        trusted_source_url,
                    )
        raise MaterialIngestError("XDW-HTTP-REDIRECT", "附件地址重定向次数过多。")

    async def _stream_to_temp(
        self,
        response: httpx.Response,
        filename: str,
        suffix: str,
        source_url: str | None,
    ) -> DownloadedFile:
        declared = response.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError:
                declared_size = -1
            if declared_size > self.max_bytes:
                raise MaterialIngestError("XDW-HTTP-SIZE", "附件超过允许的大小限制。")

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        file_descriptor, raw_path = tempfile.mkstemp(
            prefix="xdw_", suffix=f".{suffix}", dir=self.tmp_dir
        )
        os.close(file_descriptor)
        path = Path(raw_path)
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("wb") as stream:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise MaterialIngestError("XDW-HTTP-SIZE", "附件超过允许的大小限制。")
                    stream.write(chunk)
                    digest.update(chunk)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            _validate_file_type(
                path,
                suffix,
                media_type,
                max_uncompressed_bytes=min(self.max_bytes * 10, 200 * 1024 * 1024),
            )
            return DownloadedFile(
                path=path,
                source_url=source_url,
                source_format=suffix,
                filename=filename,
                mime_type=(PREFERRED_MIME[suffix] if media_type in GENERIC_MIME else media_type),
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise


class MockDownloader(MaterialDownloader):
    """仅为契约测试创建最小临时文件，不访问 source.url。"""

    async def download(self, source: AttachmentPart) -> DownloadedFile:
        source_url, filename, suffix = _source_fields(source)
        content = _mock_content(suffix)
        file_descriptor, raw_path = tempfile.mkstemp(prefix="xdw_mock_", suffix=f".{suffix}")
        os.close(file_descriptor)
        path = Path(raw_path)
        path.write_bytes(content)
        return DownloadedFile(
            path=path,
            source_url=source_url,
            source_format=suffix,
            filename=filename,
            mime_type=PREFERRED_MIME[suffix],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    async def validate_source_url(self, source: AttachmentPart) -> None:
        """测试替身不访问网络；真实环境由 SafeDownloader 执行 SSRF 校验。"""

        _source_fields(source)


def stage_uploaded_file(
    filename: str,
    data: bytes,
    *,
    max_bytes: int,
    tmp_dir: str | Path | None = None,
) -> DownloadedFile:
    """校验浏览器直传文件并暂存，供 ASR/OCR/文档 Provider 统一消费。"""

    normalized = filename.strip().replace("\\", "/")
    if (
        not normalized
        or len(normalized) > 300
        or "\x00" in normalized
        or PurePosixPath(normalized).name != normalized
        or normalized in {".", ".."}
    ):
        raise MaterialIngestError("XDW-FILE-NAME", "上传文件名无效。")
    suffix = PurePosixPath(normalized).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_MIME:
        raise MaterialIngestError("XDW-FILE-TYPE", "当前上传文件类型不受支持。")
    if not data:
        raise MaterialIngestError("XDW-FILE-EMPTY", "上传文件为空。")
    if len(data) > max_bytes:
        raise MaterialIngestError("XDW-HTTP-SIZE", "附件超过允许的大小限制。")

    destination = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.gettempdir())
    destination.mkdir(parents=True, exist_ok=True)
    file_descriptor, raw_path = tempfile.mkstemp(
        prefix="xdw_upload_", suffix=f".{suffix}", dir=destination
    )
    os.close(file_descriptor)
    path = Path(raw_path)
    try:
        path.write_bytes(data)
        _validate_file_type(
            path,
            suffix,
            "",
            max_uncompressed_bytes=min(max_bytes * 10, 200 * 1024 * 1024),
        )
        return DownloadedFile(
            path=path,
            source_format=suffix,
            filename=normalized,
            mime_type=PREFERRED_MIME[suffix],
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _source_fields(source: AttachmentPart) -> tuple[str, str, str]:
    if isinstance(source, InputAudioContentPart):
        url = source.input_audio.url
        suffix: str = source.input_audio.format
        filename = PurePosixPath(unquote(urlsplit(url).path)).name or f"audio.{suffix}"
        return url, filename, suffix
    if isinstance(source, ImageUrlContentPart):
        url = source.image_url.url
        filename = source.image_url.filename or PurePosixPath(unquote(urlsplit(url).path)).name
        suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
        if suffix not in {"png", "jpg", "jpeg", "webp"}:
            raise MaterialIngestError(
                "XDW-IMAGE-FORMAT",
                "图片地址缺少受支持的 png、jpg、jpeg 或 webp 文件扩展名。",
            )
        return url, filename, suffix
    filename = source.file.filename
    return source.file.url, filename, PurePosixPath(filename).suffix.lower().lstrip(".")


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def _validate_connected_peer(response: httpx.Response) -> None:
    network_stream = response.extensions.get("network_stream")
    if network_stream is None or not hasattr(network_stream, "get_extra_info"):
        return
    server_addr = network_stream.get_extra_info("server_addr")
    if isinstance(server_addr, tuple) and server_addr and not _is_public_ip(str(server_addr[0])):
        raise MaterialIngestError("XDW-HTTP-SSRF", "附件连接到了禁止访问的 IP。")


def _validate_file_type(
    path: Path, suffix: str, media_type: str, *, max_uncompressed_bytes: int
) -> None:
    allowed = SUPPORTED_MIME[suffix]
    if media_type not in GENERIC_MIME and media_type not in allowed:
        raise MaterialIngestError("XDW-FILE-TYPE", "附件 MIME 类型与文件扩展名不一致。")
    with path.open("rb") as stream:
        head = stream.read(16)
    valid = {
        "mp3": head.startswith(b"ID3")
        or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0),
        "wav": head.startswith(b"RIFF") and head[8:12] == b"WAVE",
        "m4a": len(head) >= 12 and head[4:8] == b"ftyp",
        "webm": head.startswith(b"\x1aE\xdf\xa3"),
        "png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": head.startswith(b"\xff\xd8\xff"),
        "jpeg": head.startswith(b"\xff\xd8\xff"),
        "webp": head.startswith(b"RIFF") and head[8:12] == b"WEBP",
        "pdf": head.startswith(b"%PDF-"),
        "docx": head.startswith(b"PK\x03\x04")
        and _is_docx(path, max_uncompressed_bytes=max_uncompressed_bytes),
        "xlsx": head.startswith(b"PK\x03\x04")
        and _is_xlsx(path, max_uncompressed_bytes=max_uncompressed_bytes),
        "txt": b"\x00" not in head,
        "md": b"\x00" not in head,
    }[suffix]
    if not valid:
        raise MaterialIngestError("XDW-FILE-HEADER", "附件内容与声明的文件格式不一致。")


def _is_docx(path: Path, *, max_uncompressed_bytes: int) -> bool:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = frozenset(item.filename for item in entries)
            safe_names = all(
                not item.filename.startswith(("/", "\\"))
                and ".." not in PurePosixPath(item.filename.replace("\\", "/")).parts
                for item in entries
            )
            return (
                len(entries) <= 10_000
                and safe_names
                and sum(item.file_size for item in entries) <= max_uncompressed_bytes
                and "[Content_Types].xml" in names
                and "word/document.xml" in names
            )
    except (BadZipFile, OSError):
        return False


def _is_xlsx(path: Path, *, max_uncompressed_bytes: int) -> bool:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = frozenset(item.filename for item in entries)
            safe_names = all(
                not item.filename.startswith(("/", "\\"))
                and ".." not in PurePosixPath(item.filename.replace("\\", "/")).parts
                for item in entries
            )
            return (
                len(entries) <= 10_000
                and safe_names
                and sum(item.file_size for item in entries) <= max_uncompressed_bytes
                and "[Content_Types].xml" in names
                and "xl/workbook.xml" in names
            )
    except (BadZipFile, OSError):
        return False


def _mock_content(suffix: str) -> bytes:
    if suffix == "wav":
        return b"RIFF\x04\x00\x00\x00WAVE"
    if suffix == "mp3":
        return b"ID3\x04\x00\x00"
    if suffix == "m4a":
        return b"\x00\x00\x00\x18ftypM4A "
    if suffix == "webm":
        return b"\x1aE\xdf\xa3"
    if suffix in {"txt", "md"}:
        return "Mock 文档".encode()
    if suffix == "pdf":
        return b"%PDF-1.7\n"
    if suffix == "png":
        return b"\x89PNG\r\n\x1a\n"
    if suffix in {"jpg", "jpeg"}:
        return b"\xff\xd8\xff\xe0"
    if suffix == "webp":
        return b"RIFF\x00\x00\x00\x00WEBP"
    return b"PK\x03\x04"
