from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import pytest
from docx import Document

from app.multimodal.contracts import FileContentPart
from app.multimodal.downloader import SafeDownloader
from app.multimodal.errors import MaterialIngestError


async def public_resolver(hostname: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def file_part(filename: str, url: str | None = None) -> FileContentPart:
    return FileContentPart.model_validate(
        {
            "type": "file",
            "file": {
                "url": url or f"https://files.example.org/{filename}",
                "filename": filename,
            },
        }
    )


def downloader(
    handler: httpx.AsyncBaseTransport | httpx.MockTransport,
    tmp_path: Path,
    *,
    max_bytes: int = 1024,
    resolver=public_resolver,
    retries: int = 0,
) -> SafeDownloader:
    return SafeDownloader(
        max_bytes=max_bytes,
        tmp_dir=tmp_path,
        resolver=resolver,
        transport=handler,
        retries=retries,
    )


async def test_downloads_streams_validates_and_returns_no_source_url(tmp_path: Path) -> None:
    content = "真实访谈正文".encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-length": str(len(content))},
            content=content,
        )
    )
    result = await downloader(transport, tmp_path).download(file_part("材料.txt"))
    try:
        assert result.path.read_bytes() == content
        assert result.size_bytes == len(content)
        assert len(result.sha256) == 64
        assert "url" not in result.model_dump()
    finally:
        result.path.unlink()


async def test_rejects_private_dns_and_private_redirect_without_requesting_target(
    tmp_path: Path,
) -> None:
    async def private_resolver(hostname: str, port: int) -> list[str]:
        return ["10.0.0.8"]

    blocked = downloader(httpx.MockTransport(lambda request: httpx.Response(200)), tmp_path, resolver=private_resolver)
    with pytest.raises(MaterialIngestError) as caught:
        await blocked.validate_url("https://private.example.org/a.txt")
    assert caught.value.code == "XDW-HTTP-SSRF"

    requests: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret.txt"})

    safe = downloader(httpx.MockTransport(redirect), tmp_path)
    with pytest.raises(MaterialIngestError) as redirected:
        await safe.download(file_part("a.txt"))
    assert redirected.value.code == "XDW-HTTP-SSRF"
    assert requests == ["https://files.example.org/a.txt"]


async def test_rejects_private_actual_peer_after_public_dns(tmp_path: Path) -> None:
    class PrivatePeer:
        def get_extra_info(self, name: str):
            return ("127.0.0.1", 443) if name == "server_addr" else None

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"hidden",
            extensions={"network_stream": PrivatePeer()},
        )
    )
    with pytest.raises(MaterialIngestError) as caught:
        await downloader(transport, tmp_path).download(file_part("a.txt"))
    assert caught.value.code == "XDW-HTTP-SSRF"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("headers", "content", "code"),
    [
        ({"content-type": "text/plain", "content-length": "200"}, b"short", "XDW-HTTP-SIZE"),
        ({"content-type": "text/plain"}, b"x" * 20, "XDW-HTTP-SIZE"),
        ({"content-type": "application/pdf"}, b"plain text", "XDW-FILE-TYPE"),
    ],
)
async def test_size_and_mime_failures_remove_temp_files(
    tmp_path: Path,
    headers: dict[str, str],
    content: bytes,
    code: str,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, content=content)
    )
    with pytest.raises(MaterialIngestError) as caught:
        await downloader(transport, tmp_path, max_bytes=10).download(file_part("a.txt"))
    assert caught.value.code == code
    assert list(tmp_path.iterdir()) == []


async def test_docx_requires_real_office_zip_structure(tmp_path: Path) -> None:
    invalid_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/zip"},
            content=b"PK\x03\x04not-a-docx",
        )
    )
    with pytest.raises(MaterialIngestError) as invalid:
        await downloader(invalid_transport, tmp_path).download(file_part("a.docx"))
    assert invalid.value.code == "XDW-FILE-HEADER"

    document = Document()
    document.add_paragraph("真实文档")
    stream = BytesIO()
    document.save(stream)
    valid_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/zip"},
            content=stream.getvalue(),
        )
    )
    result = await downloader(valid_transport, tmp_path, max_bytes=100_000).download(
        file_part("a.docx")
    )
    result.path.unlink()


async def test_retries_retryable_network_failure_once(tmp_path: Path) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    result = await downloader(
        httpx.MockTransport(flaky), tmp_path, retries=1
    ).download(file_part("a.txt"))
    try:
        assert calls == 2
    finally:
        result.path.unlink()
