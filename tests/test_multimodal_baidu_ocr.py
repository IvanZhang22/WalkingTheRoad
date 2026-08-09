from __future__ import annotations

import io
import random
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile
from app.multimodal.providers.baidu_ocr import BaiduOCRProvider


def source(tmp_path: Path, filename: str, content: bytes) -> DownloadedFile:
    path = tmp_path / filename
    path.write_bytes(content)
    return DownloadedFile(
        path=path,
        source_url=f"https://files.example.org/{filename}?signature=secret",
        source_format=Path(filename).suffix.lower().lstrip("."),
        filename=filename,
        mime_type="application/pdf" if filename.endswith(".pdf") else "image/png",
        size_bytes=len(content),
        sha256="b" * 64,
    )


def provider(handler: httpx.MockTransport, **kwargs: object) -> BaiduOCRProvider:
    return BaiduOCRProvider(
        api_key="test-ak",
        secret_key="test-sk",
        transport=handler,
        **kwargs,
    )


async def test_image_authenticates_and_normalizes_bbox_and_confidence(tmp_path: Path) -> None:
    requests: list[tuple[str, dict[str, list[str]], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        requests.append((request.url.path, form, request.url.query.decode()))
        if request.url.path.endswith("/token"):
            assert form["client_id"] == ["test-ak"]
            assert form["client_secret"] == ["test-sk"]
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "words_result_num": 2,
                "words_result": [
                    {
                        "words": "就业服务入口不明显",
                        "location": {"left": 10, "top": 20, "width": 300, "height": 40},
                        "probability": {"average": 0.96, "min": 0.9, "variance": 0.01},
                    },
                    {
                        "words": "需要反复寻找",
                        "location": {"left": 10, "top": 70, "width": 240, "height": 35},
                        "probability": {"average": 0.72},
                    },
                ],
            },
        )

    result = await provider(httpx.MockTransport(handler)).recognize(
        source(tmp_path, "notice.png", b"png-content")
    )

    assert result.provider_name == "baidu"
    assert result.provider_model == "general"
    assert result.normalized_text == "就业服务入口不明显\n需要反复寻找"
    assert result.segments[0].locator.bbox == (10.0, 20.0, 300.0, 40.0)
    assert result.segments[0].locator.page is None
    assert result.segments[0].confidence == 0.96
    assert result.segments[1].confidence == 0.72
    assert requests[1][1]["probability"] == ["true"]
    assert requests[1][1]["detect_direction"] == ["true"]
    assert requests[1][2] == "access_token=token-1"


async def test_pp_ocr_v6_request_and_response_are_normalized(tmp_path: Path) -> None:
    ocr_forms: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "token-1", "expires_in": 3600}
            )
        assert request.url.path.endswith("/pp_ocrv5")
        ocr_forms.append(form)
        return httpx.Response(
            200,
            json={
                "page_result": [
                    {
                        "lines": ["就业服务入口", "需要反复寻找"],
                        "probability": [0.98, 0.87],
                        "rec_boxes": [[10, 20, 310, 60], [10, 70, 250, 105]],
                        "rec_polys": [
                            [[10, 20], [310, 20], [310, 60], [10, 60]],
                            [[10, 70], [250, 70], [250, 105], [10, 105]],
                        ],
                    }
                ]
            },
        )

    result = await provider(
        httpx.MockTransport(handler),
        endpoint_path="/rest/2.0/ocr/v1/pp_ocrv5",
    ).recognize(source(tmp_path, "notice.png", b"png-content"))

    assert result.provider_model == "pp_ocrv5"
    assert result.normalized_text == "就业服务入口\n需要反复寻找"
    assert result.segments[0].locator.bbox == (10.0, 20.0, 300.0, 40.0)
    assert result.segments[0].confidence == 0.98
    assert result.segments[1].locator.bbox == (10.0, 70.0, 240.0, 35.0)
    assert result.segments[1].confidence == 0.87
    assert ocr_forms[0]["useDocOrientationClassify"] == ["true"]
    assert ocr_forms[0]["useDocUnwarping"] == ["false"]
    assert ocr_forms[0]["useTextlineOrientation"] == ["true"]
    assert "language_type" not in ocr_forms[0]
    assert "probability" not in ocr_forms[0]


async def test_pp_ocr_v6_uses_polygon_when_box_is_invalid(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "token-1", "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json={
                "page_result": [
                    {
                        "lines": ["倾斜文本"],
                        "probability": [1.5],
                        "rec_boxes": [[20, 10, 10, 30]],
                        "rec_polys": [[[8, 10], [22, 8], [24, 30], [10, 32]]],
                    }
                ]
            },
        )

    result = await provider(
        httpx.MockTransport(handler),
        endpoint_path="/rest/2.0/ocr/v1/pp_ocrv5",
    ).recognize(source(tmp_path, "notice.png", b"png-content"))

    assert result.segments[0].locator.bbox == (8.0, 8.0, 16.0, 24.0)
    assert result.segments[0].confidence is None


async def test_large_png_is_reencoded_before_upload(tmp_path: Path) -> None:
    ocr_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ocr_requests
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "token-1", "expires_in": 3600}
            )
        ocr_requests += 1
        assert len(request.content) < 4 * 1024 * 1024
        form = parse_qs(request.content.decode())
        assert form["image"][0].startswith("/9j/")
        return httpx.Response(
            200,
            json={
                "page_result": [
                    {
                        "lines": ["三兆图片识别成功"],
                        "probability": [0.99],
                        "rec_boxes": [[1, 2, 201, 42]],
                    }
                ]
            },
        )

    pixels = random.Random(20260809).randbytes(1200 * 900 * 3)
    image = Image.frombytes("RGB", (1200, 900), pixels)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    result = await provider(
        httpx.MockTransport(handler),
        endpoint_path="/rest/2.0/ocr/v1/pp_ocrv5",
    ).recognize(source(tmp_path, "large.png", content))

    assert 3_000_000 < len(content) < 4_000_000
    assert ocr_requests == 1
    assert result.normalized_text == "三兆图片识别成功"


async def test_scanned_pdf_is_submitted_page_by_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf_path)
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600})
        page = form["pdf_file_num"][0]
        pages.append(page)
        assert "pdf_file" in form
        assert "image" not in form
        return httpx.Response(
            200,
            json={
                "pdf_file_size": "2",
                "words_result": [
                    {
                        "words": f"第{page}页",
                        "location": {"left": 1, "top": 2, "width": 30, "height": 10},
                        "probability": {"average": 0.91},
                    }
                ],
            },
        )

    downloaded = source(tmp_path, "scan.pdf", pdf_path.read_bytes())
    downloaded.path = pdf_path
    result = await provider(httpx.MockTransport(handler)).recognize(downloaded)

    assert pages == ["1", "2"]
    assert [item.locator.page for item in result.segments] == [1, 2]
    assert [item.text for item in result.segments] == ["第1页", "第2页"]


async def test_expired_token_is_refreshed_once(tmp_path: Path) -> None:
    token_calls = 0
    ocr_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, ocr_calls
        if request.url.path.endswith("/token"):
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{token_calls}", "expires_in": 3600},
            )
        ocr_calls += 1
        if ocr_calls == 1:
            return httpx.Response(200, json={"error_code": 110, "error_msg": "secret"})
        return httpx.Response(
            200,
            json={
                "words_result": [
                    {
                        "words": "刷新成功",
                        "location": {"left": 1, "top": 2, "width": 3, "height": 4},
                        "probability": {"average": 0.99},
                    }
                ]
            },
        )

    result = await provider(httpx.MockTransport(handler)).recognize(
        source(tmp_path, "scan.png", b"png")
    )
    assert result.normalized_text == "刷新成功"
    assert token_calls == 2
    assert ocr_calls == 2


async def test_missing_credentials_and_form_limit_fail_closed(tmp_path: Path) -> None:
    downloaded = source(tmp_path, "scan.png", b"png")
    with pytest.raises(MaterialIngestError) as missing:
        await BaiduOCRProvider(api_key="", secret_key="").recognize(downloaded)
    assert missing.value.code == "XDW-OCR-NOT-CONFIGURED"

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    with pytest.raises(MaterialIngestError) as too_large:
        await provider(httpx.MockTransport(handler), max_form_bytes=10).recognize(downloaded)
    assert too_large.value.code == "XDW-OCR-FORM-SIZE"
    assert calls == 1


@pytest.mark.parametrize("status_code", [401, 429, 500])
async def test_http_failures_never_expose_credentials_or_vendor_body(
    tmp_path: Path, status_code: int
) -> None:
    ocr = provider(
        httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"error": "secret vendor detail"})
        )
    )
    with pytest.raises(MaterialIngestError) as caught:
        await ocr.recognize(source(tmp_path, "scan.png", b"png"))
    assert caught.value.code == "XDW-OCR-UPSTREAM-HTTP"
    assert "test-ak" not in caught.value.public_message
    assert "test-sk" not in caught.value.public_message
    assert "secret vendor detail" not in caught.value.public_message


async def test_invalid_bbox_or_missing_confidence_is_preserved_for_review(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "words_result": [
                    {
                        "words": "无效坐标",
                        "location": {"left": -1, "top": 2, "width": 3, "height": 4},
                        "probability": {"average": 0.99},
                    },
                    {
                        "words": "缺少置信度",
                        "location": {"left": 1, "top": 2, "width": 3, "height": 4},
                    },
                ]
            },
        )

    result = await provider(httpx.MockTransport(handler)).recognize(
        source(tmp_path, "scan.png", b"png")
    )
    assert [item.text for item in result.segments] == ["缺少置信度"]
    assert result.segments[0].confidence is None
