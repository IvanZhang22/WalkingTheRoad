"""百度智能云通用文字识别（含位置）Provider。"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import (
    DownloadedFile,
    MaterialLocator,
    ProviderResult,
    ProviderSegment,
)
from app.multimodal.providers.base import OCRProvider

TOKEN_PATH = "/oauth/2.0/token"
IMAGE_FORMATS = frozenset({"png", "jpg", "jpeg", "webp"})
TOKEN_ERROR_CODES = frozenset({110, 111})
PP_OCR_MAX_FORM_BYTES = 10_000_000
IMAGE_REENCODE_THRESHOLD_BYTES = 1_500_000
IMAGE_JPEG_QUALITY = 85


class BaiduOCRProvider(OCRProvider):
    """上传图片或逐页提交扫描 PDF，并归一化行置信度与矩形 bbox。"""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str = "https://aip.baidubce.com",
        endpoint_path: str = "/rest/2.0/ocr/v1/general",
        timeout: float = 60,
        max_pages: int = 20,
        max_form_bytes: int = PP_OCR_MAX_FORM_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout <= 0 or max_pages <= 0 or max_form_bytes <= 0:
            raise ValueError("百度 OCR 超时、页数和请求大小限制必须大于 0")
        if not endpoint_path.startswith("/"):
            raise ValueError("百度 OCR endpoint_path 必须以 / 开头")
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.base_url = base_url.rstrip("/")
        self.endpoint_path = endpoint_path
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_form_bytes = max_form_bytes
        self.transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def recognize(self, source: DownloadedFile) -> ProviderResult:
        if not self.api_key or not self.secret_key:
            raise MaterialIngestError(
                "XDW-OCR-NOT-CONFIGURED",
                "百度 OCR API Key 或 Secret Key 尚未配置。",
            )
        source_format = source.source_format or PurePosixPath(
            source.filename
        ).suffix.lower().lstrip(".")
        if source_format not in IMAGE_FORMATS | {"pdf"}:
            raise MaterialIngestError(
                "XDW-OCR-FORMAT",
                "百度 OCR 当前只接收 png、jpg、jpeg、webp 或扫描 PDF。",
            )
        raw = await asyncio.to_thread(source.path.read_bytes)
        if not raw:
            raise MaterialIngestError("XDW-OCR-EMPTY-SOURCE", "OCR 输入文件为空。")
        if source_format in IMAGE_FORMATS:
            raw = await asyncio.to_thread(
                _prepare_image_for_ocr,
                raw,
                source_format,
            )

        page_count = 1
        if source_format == "pdf":
            page_count = await asyncio.to_thread(_pdf_page_count, source)
            if page_count > self.max_pages:
                raise MaterialIngestError(
                    "XDW-OCR-PAGE-LIMIT",
                    f"扫描 PDF 超过 OCR 的 {self.max_pages} 页处理上限。",
                )

        timeout = httpx.Timeout(self.timeout)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
            headers={"User-Agent": "Xingxiaodao-Agent/2.2"},
        ) as client:
            token = await self._access_token(client)
            segments: list[ProviderSegment] = []
            for page in range(1, page_count + 1):
                form = _request_form(
                    raw,
                    source_format,
                    page,
                    pp_ocr=_is_pp_ocr_endpoint(self.endpoint_path),
                )
                self._validate_form_size(form)
                payload = await self._post_ocr(client, token, form)
                if _error_code(payload) in TOKEN_ERROR_CODES:
                    self._invalidate_token()
                    token = await self._access_token(client)
                    payload = await self._post_ocr(client, token, form)
                self._raise_api_error(payload)
                segments.extend(
                    _segments_from_response(
                        payload,
                        page=page if source_format == "pdf" else None,
                    )
                )

        if not segments:
            raise MaterialIngestError(
                "XDW-OCR-EMPTY",
                "OCR 没有识别出可用文字。",
            )
        return ProviderResult(
            provider_name="baidu",
            provider_model=PurePosixPath(self.endpoint_path).name,
            normalized_text="\n".join(item.text for item in segments),
            segments=segments,
        )

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            payload = await self._post_form_json(
                client,
                TOKEN_PATH,
                {
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.secret_key,
                },
                error_prefix="鉴权",
            )
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not isinstance(token, str) or not token.strip():
                raise MaterialIngestError(
                    "XDW-OCR-AUTH",
                    "百度 OCR 鉴权失败，请检查服务端环境变量。",
                )
            lifetime = (
                float(expires_in)
                if isinstance(expires_in, (int, float))
                and not isinstance(expires_in, bool)
                and expires_in > 120
                else 3600.0
            )
            self._token = token
            self._token_expires_at = time.monotonic() + lifetime - 60
            return token

    async def _post_ocr(
        self, client: httpx.AsyncClient, token: str, form: dict[str, str]
    ) -> dict[str, Any]:
        return await self._post_form_json(
            client,
            self.endpoint_path,
            form,
            params={"access_token": token},
            error_prefix="识别",
        )

    async def _post_form_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        form: dict[str, str],
        *,
        error_prefix: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await client.post(path, data=form, params=params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MaterialIngestError(
                "XDW-OCR-TRANSPORT",
                f"百度 OCR {error_prefix}请求失败，请稍后重试。",
                retryable=True,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise MaterialIngestError(
                "XDW-OCR-UPSTREAM-HTTP",
                f"百度 OCR {error_prefix}返回 HTTP {response.status_code}。",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MaterialIngestError(
                "XDW-OCR-BAD-RESPONSE",
                f"百度 OCR {error_prefix}返回了无法解析的响应。",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise MaterialIngestError(
                "XDW-OCR-BAD-RESPONSE",
                f"百度 OCR {error_prefix}返回结构无效。",
                retryable=True,
            )
        return payload

    def _validate_form_size(self, form: dict[str, str]) -> None:
        if len(urlencode(form).encode("ascii")) > self.max_form_bytes:
            limit_mb = self.max_form_bytes / 1_000_000
            raise MaterialIngestError(
                "XDW-OCR-FORM-SIZE",
                f"OCR 文件编码后超过百度接口 {limit_mb:g} MB 请求大小限制，"
                "请压缩或缩小图片后重试。",
            )

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    def _raise_api_error(self, payload: dict[str, Any]) -> None:
        code = _error_code(payload)
        if code is None:
            return
        raise MaterialIngestError(
            "XDW-OCR-UPSTREAM",
            "百度 OCR 未能完成识别，请检查权限、额度或输入文件。",
            retryable=code in {4, 17, 18, 110, 111},
        )


def _pdf_page_count(source: DownloadedFile) -> int:
    try:
        count = len(PdfReader(source.path).pages)
    except Exception as exc:
        raise MaterialIngestError("XDW-OCR-PDF", "扫描 PDF 无法读取或已损坏。") from exc
    if count <= 0:
        raise MaterialIngestError("XDW-OCR-PDF", "扫描 PDF 没有可识别页面。")
    return count


def _prepare_image_for_ocr(raw: bytes, source_format: str) -> bytes:
    if (
        len(raw) < IMAGE_REENCODE_THRESHOLD_BYTES
        and source_format in {"png", "jpg", "jpeg"}
    ):
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.width < 15 or image.height < 15:
                raise MaterialIngestError(
                    "XDW-OCR-IMAGE-DIMENSIONS",
                    "OCR 图片最短边必须至少为 15 像素。",
                )
            if image.width > 8192 or image.height > 8192:
                raise MaterialIngestError(
                    "XDW-OCR-IMAGE-DIMENSIONS",
                    "OCR 图片最长边不能超过 8192 像素，请缩小图片后重试。",
                )
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = image.convert("RGB")
            output = io.BytesIO()
            rgb.save(
                output,
                format="JPEG",
                quality=IMAGE_JPEG_QUALITY,
                optimize=True,
                progressive=True,
                subsampling=1,
            )
            return output.getvalue()
    except MaterialIngestError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MaterialIngestError(
            "XDW-OCR-IMAGE",
            "OCR 图片无法读取或已损坏。",
        ) from exc


def _is_pp_ocr_endpoint(endpoint_path: str) -> bool:
    return PurePosixPath(endpoint_path).name.startswith("pp_ocr")


def _request_form(
    raw: bytes,
    source_format: str,
    page: int,
    *,
    pp_ocr: bool = False,
) -> dict[str, str]:
    encoded = base64.b64encode(raw).decode("ascii")
    form = {"image" if source_format != "pdf" else "pdf_file": encoded}
    if pp_ocr:
        form.update(
            {
                "useDocOrientationClassify": "true",
                "useDocUnwarping": "false",
                "useTextlineOrientation": "true",
            }
        )
    else:
        form.update(
            {
                "language_type": "CHN_ENG",
                "detect_direction": "true",
                "probability": "true",
            }
        )
    if source_format == "pdf":
        form["pdf_file_num"] = str(page)
    return form


def _segments_from_response(
    payload: dict[str, Any], *, page: int | None
) -> list[ProviderSegment]:
    if "page_result" in payload:
        return _segments_from_pp_ocr(payload, page=page)

    raw_items = payload.get("words_result")
    if not isinstance(raw_items, list):
        raise MaterialIngestError(
            "XDW-OCR-BAD-RESPONSE",
            "百度 OCR 响应缺少文字结果数组。",
            retryable=True,
        )
    segments: list[ProviderSegment] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = item.get("words")
        location = item.get("location")
        if not isinstance(text, str) or not text.strip() or not isinstance(location, dict):
            continue
        bbox = _bbox(location)
        if bbox is None:
            continue
        segments.append(
            ProviderSegment(
                text=text.strip(),
                confidence=_confidence(item.get("probability")),
                locator=MaterialLocator(page=page, bbox=bbox),
            )
        )
    return segments


def _segments_from_pp_ocr(
    payload: dict[str, Any], *, page: int | None
) -> list[ProviderSegment]:
    raw_pages = payload.get("page_result")
    if not isinstance(raw_pages, list):
        raise MaterialIngestError(
            "XDW-OCR-BAD-RESPONSE",
            "百度 OCR 响应缺少页面结果数组。",
            retryable=True,
        )

    segments: list[ProviderSegment] = []
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            continue
        lines = raw_page.get("lines")
        probabilities = raw_page.get("probability")
        boxes = raw_page.get("rec_boxes")
        polygons = raw_page.get("rec_polys")
        if not isinstance(lines, list):
            continue
        for index, text in enumerate(lines):
            if not isinstance(text, str) or not text.strip():
                continue
            bbox = _indexed_bbox(boxes, polygons, index)
            if bbox is None:
                continue
            confidence = None
            if isinstance(probabilities, list) and index < len(probabilities):
                confidence = _scalar_confidence(probabilities[index])
            segments.append(
                ProviderSegment(
                    text=text.strip(),
                    confidence=confidence,
                    locator=MaterialLocator(page=page, bbox=bbox),
                )
            )
    return segments


def _indexed_bbox(
    boxes: Any, polygons: Any, index: int
) -> tuple[float, float, float, float] | None:
    if isinstance(boxes, list) and index < len(boxes):
        bbox = _bbox_from_box(boxes[index])
        if bbox is not None:
            return bbox
    if isinstance(polygons, list) and index < len(polygons):
        return _bbox_from_polygon(polygons[index])
    return None


def _bbox_from_box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(_is_nonnegative_number(item) for item in value):
        return None
    left, top, right, bottom = (float(item) for item in value)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    return (left, top, width, height)


def _bbox_from_polygon(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    points: list[tuple[float, float]] = []
    for point in value:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or not all(_is_nonnegative_number(item) for item in point)
        ):
            return None
        points.append((float(point[0]), float(point[1])))
    left = min(point[0] for point in points)
    top = min(point[1] for point in points)
    right = max(point[0] for point in points)
    bottom = max(point[1] for point in points)
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


def _is_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def _bbox(location: dict[str, Any]) -> tuple[float, float, float, float] | None:
    values = [location.get(key) for key in ("left", "top", "width", "height")]
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
        for value in values
    ):
        return None
    left, top, width, height = (float(cast(int | float, value)) for value in values)
    if width <= 0 or height <= 0:
        return None
    return (left, top, width, height)


def _confidence(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    average = value.get("average")
    if (
        not isinstance(average, (int, float))
        or isinstance(average, bool)
        or not math.isfinite(float(average))
        or not 0 <= average <= 1
    ):
        return None
    return float(average)


def _scalar_confidence(value: Any) -> float | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 <= value <= 1
    ):
        return None
    return float(value)


def _error_code(payload: dict[str, Any]) -> int | None:
    value = payload.get("error_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
