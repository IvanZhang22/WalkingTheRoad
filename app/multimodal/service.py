"""多附件隔离处理、统一归一化与低置信证据门控。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit

from app.multimodal.contracts import (
    IMAGE_FORMATS,
    FileContentPart,
    InputAudioContentPart,
)
from app.multimodal.downloader import MaterialDownloader, MockDownloader, SafeDownloader
from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import (
    DownloadedFile,
    Material,
    MaterialIssue,
    MaterialModality,
    MaterialSegment,
    MaterialStatus,
    ProviderResult,
    ProviderSegment,
)
from app.multimodal.providers.base import ASRProvider, DocumentParser, OCRProvider
from app.multimodal.providers.document import LocalDocumentParser
from app.multimodal.providers.mock import MockASRProvider, MockDocumentParser, MockOCRProvider
from app.multimodal.providers.unavailable import UnavailableASRProvider, UnavailableOCRProvider

AttachmentPart = InputAudioContentPart | FileContentPart


class MaterialIngestService:
    def __init__(
        self,
        *,
        downloader: MaterialDownloader,
        asr: ASRProvider,
        ocr: OCRProvider,
        document_parser: DocumentParser,
        confidence_threshold: float = 0.8,
        enabled_modalities: frozenset[MaterialModality] | None = None,
    ) -> None:
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold 必须位于 0 到 1 之间")
        self.downloader = downloader
        self.asr = asr
        self.ocr = ocr
        self.document_parser = document_parser
        self.confidence_threshold = confidence_threshold
        self.enabled_modalities = (
            frozenset(MaterialModality) if enabled_modalities is None else enabled_modalities
        )

    async def ingest(self, attachments: list[AttachmentPart]) -> list[Material]:
        """并发处理附件；单个失败不会取消其它附件。"""

        return list(await asyncio.gather(*(self._ingest_isolated(item) for item in attachments)))

    async def _ingest_isolated(self, attachment: AttachmentPart) -> Material:
        material_id, fingerprint = _material_identity(attachment)
        filename, modality = _attachment_identity(attachment)
        downloaded = None
        try:
            if modality not in self.enabled_modalities:
                raise MaterialIngestError(
                    "XDW-MM-PROVIDER-NOT-CONFIGURED",
                    f"{modality.value} 材料的真实 Provider 尚未配置。",
                )
            downloaded = await self.downloader.download(attachment)
            result = await self._call_provider(downloaded, modality)
            return self._normalize_result(
                material_id=material_id,
                fingerprint=fingerprint,
                filename=filename,
                modality=modality,
                result=result,
            )
        except MaterialIngestError as exc:
            issue = MaterialIssue(
                code=exc.code,
                message=exc.public_message,
                retryable=exc.retryable,
            )
        except Exception:
            issue = MaterialIssue(
                code="XDW-MM-UNEXPECTED",
                message="材料处理失败；服务端没有返回可用内容。",
                retryable=False,
            )
        finally:
            if downloaded is not None:
                downloaded.path.unlink(missing_ok=True)
        return Material(
            material_id=material_id,
            source_fingerprint=fingerprint,
            filename=filename,
            modality=modality,
            status=MaterialStatus.failed,
            issues=[issue],
        )

    async def _call_provider(
        self, downloaded: DownloadedFile, modality: MaterialModality
    ) -> ProviderResult:
        if modality is MaterialModality.audio:
            return await self.asr.transcribe(downloaded)
        if modality is MaterialModality.image:
            return await self.ocr.recognize(downloaded)
        return await self.document_parser.parse(downloaded)

    def _normalize_result(
        self,
        *,
        material_id: str,
        fingerprint: str,
        filename: str,
        modality: MaterialModality,
        result: ProviderResult,
    ) -> Material:
        segments = [
            self._normalize_segment(material_id, modality, index, segment)
            for index, segment in enumerate(result.segments, start=1)
        ]
        if not segments:
            return Material(
                material_id=material_id,
                source_fingerprint=fingerprint,
                filename=filename,
                modality=modality,
                status=MaterialStatus.failed,
                provider_name=result.provider_name,
                provider_model=result.provider_model,
                warnings=result.warnings,
                issues=[
                    MaterialIssue(
                        code="XDW-MM-EMPTY",
                        message="识别或解析结果为空，不能进入证据分析。",
                    )
                ],
            )

        usable = [segment for segment in segments if segment.automatic_evidence_use]
        review_queue = [
            segment.segment_id for segment in segments if not segment.automatic_evidence_use
        ]
        normalized_text = result.normalized_text or "\n".join(
            segment.text for segment in segments
        )
        automatic_text = (
            normalized_text
            if len(usable) == len(segments)
            else "\n".join(segment.text for segment in usable)
        )
        return Material(
            material_id=material_id,
            source_fingerprint=fingerprint,
            filename=filename,
            modality=modality,
            status=MaterialStatus.ready if not review_queue else MaterialStatus.manual_review,
            normalized_text=normalized_text,
            automatic_text=automatic_text,
            provider_name=result.provider_name,
            provider_model=result.provider_model,
            segments=segments,
            automatic_evidence_use=bool(usable),
            review_queue=review_queue,
            warnings=result.warnings,
        )

    def _normalize_segment(
        self,
        material_id: str,
        modality: MaterialModality,
        index: int,
        segment: ProviderSegment,
    ) -> MaterialSegment:
        flags: list[str] = []
        if segment.confidence is None:
            flags.append("confidence_missing")
        elif segment.confidence < self.confidence_threshold:
            flags.append("low_confidence")
        if not _has_required_locator(modality, segment):
            flags.append("location_missing")
        return MaterialSegment(
            segment_id=f"SEG_{material_id.removeprefix('MAT_')}_{index:04d}",
            material_id=material_id,
            modality=modality,
            text=segment.text,
            provider_confidence=segment.confidence,
            locator=segment.locator,
            automatic_evidence_use=not flags,
            quality_flags=flags,
            speaker=segment.speaker,
        )


def _attachment_identity(attachment: AttachmentPart) -> tuple[str, MaterialModality]:
    if isinstance(attachment, InputAudioContentPart):
        filename = PurePosixPath(unquote(urlsplit(attachment.input_audio.url).path)).name
        return filename or f"audio.{attachment.input_audio.format}", MaterialModality.audio
    filename = attachment.file.filename
    suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
    modality = MaterialModality.image if suffix in IMAGE_FORMATS else MaterialModality.document
    return filename, modality


def _material_identity(attachment: AttachmentPart) -> tuple[str, str]:
    if isinstance(attachment, InputAudioContentPart):
        source = (
            f"audio\0{_stable_url(attachment.input_audio.url)}\0{attachment.input_audio.format}"
        )
    else:
        source = f"file\0{_stable_url(attachment.file.url)}\0{attachment.file.filename}"
    fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"MAT_{fingerprint[:12].upper()}", fingerprint


def _stable_url(url: str) -> str:
    """签名参数轮换时保持同一对象的材料 ID 稳定，且不把参数写入指纹来源。"""

    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))


def _has_required_locator(modality: MaterialModality, segment: ProviderSegment) -> bool:
    locator = segment.locator
    if modality is MaterialModality.audio:
        return locator.start_ms is not None and locator.end_ms is not None
    if modality is MaterialModality.image:
        return locator.bbox is not None
    return (
        locator.page is not None
        or (locator.char_start is not None and locator.char_end is not None)
    )


def build_mock_ingest_service() -> MaterialIngestService:
    return MaterialIngestService(
        downloader=MockDownloader(),
        asr=MockASRProvider(),
        ocr=MockOCRProvider(),
        document_parser=MockDocumentParser(),
    )


def build_document_ingest_service(
    *,
    max_upload_bytes: int,
    max_document_chars: int,
    connect_timeout: float,
    read_timeout: float,
    max_redirects: int,
) -> MaterialIngestService:
    """v2.2.1 Live 服务：只开放普通文档，音频和图片继续失败关闭。"""

    return MaterialIngestService(
        downloader=SafeDownloader(
            max_bytes=max_upload_bytes,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_redirects=max_redirects,
        ),
        asr=UnavailableASRProvider(),
        ocr=UnavailableOCRProvider(),
        document_parser=LocalDocumentParser(max_document_chars=max_document_chars),
        enabled_modalities=frozenset({MaterialModality.document}),
    )


def format_material_summary(materials: list[Material]) -> str:
    ready = sum(material.status is MaterialStatus.ready for material in materials)
    review = sum(material.status is MaterialStatus.manual_review for material in materials)
    failed = sum(material.status is MaterialStatus.failed for material in materials)
    lines = [
        "多模态材料接入预检（当前不回显原始 URL 或材料全文）：",
        f"- 共 {len(materials)} 份；自动可用 {ready} 份；待人工复核 {review} 份；失败 {failed} 份。",
    ]
    for material in materials:
        lines.append(
            f"- `{material.material_id}`｜{material.filename}｜{material.modality.value}"
            f"｜{material.status.value}｜片段 {len(material.segments)}"
        )
    return "\n".join(lines)
