from __future__ import annotations

from app.multimodal.contracts import FileContentPart, InputAudioContentPart
from app.multimodal.downloader import MockDownloader
from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import (
    DownloadedFile,
    MaterialLocator,
    MaterialModality,
    MaterialStatus,
    ProviderResult,
    ProviderSegment,
)
from app.multimodal.providers.baidu_ocr import BaiduOCRProvider
from app.multimodal.providers.base import ASRProvider, DocumentParser, OCRProvider
from app.multimodal.providers.mock import MockDocumentParser, MockOCRProvider
from app.multimodal.providers.stepfun_asr import StepFunASRProvider
from app.multimodal.providers.unavailable import UnavailableASRProvider, UnavailableOCRProvider
from app.multimodal.service import (
    MaterialIngestService,
    build_live_ingest_service,
    build_mock_ingest_service,
)


def audio(url: str = "https://files.example.org/interview.mp3") -> InputAudioContentPart:
    return InputAudioContentPart.model_validate(
        {"type": "input_audio", "input_audio": {"url": url, "format": "mp3"}}
    )


def file(filename: str) -> FileContentPart:
    return FileContentPart.model_validate(
        {
            "type": "file",
            "file": {"url": f"https://files.example.org/{filename}", "filename": filename},
        }
    )


async def test_mock_service_normalizes_all_v22_modalities() -> None:
    materials = await build_mock_ingest_service().ingest(
        [audio(), file("现场照片.png"), file("访谈材料.pdf")]
    )
    assert [item.modality.value for item in materials] == ["audio", "image", "document"]
    assert all(item.status is MaterialStatus.ready for item in materials)
    assert all(item.automatic_evidence_use for item in materials)
    assert materials[0].segments[0].locator.end_ms == 2500
    assert materials[1].segments[0].locator.bbox == (100.0, 100.0, 600.0, 80.0)
    assert materials[2].segments[0].locator.page == 1
    assert all("https://" not in item.model_dump_json() for item in materials)


async def test_signed_url_rotation_keeps_material_identity_stable() -> None:
    service = build_mock_ingest_service()
    first = (await service.ingest([audio("https://files.example.org/interview.mp3?token=first")]))[
        0
    ]
    second = (
        await service.ingest([audio("https://files.example.org/interview.mp3?token=second")])
    )[0]
    assert first.material_id == second.material_id
    assert first.source_fingerprint == second.source_fingerprint


class LowConfidenceASR(ASRProvider):
    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        return ProviderResult(
            provider_name="test",
            provider_model="low-confidence",
            segments=[
                ProviderSegment(
                    text="可能识别错误的内容",
                    confidence=0.42,
                    locator=MaterialLocator(start_ms=0, end_ms=1000),
                )
            ],
        )


class FailingASR(ASRProvider):
    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        raise RuntimeError("secret upstream detail")


class OCRRequiredParser(DocumentParser):
    async def parse(self, source: DownloadedFile) -> ProviderResult:
        raise MaterialIngestError("XDW-DOC-OCR-REQUIRED", "需要 OCR")


class LocatedOCR(OCRProvider):
    async def recognize(self, source: DownloadedFile) -> ProviderResult:
        return ProviderResult(
            provider_name="test-ocr",
            provider_model="located",
            segments=[
                ProviderSegment(
                    text="扫描页正文",
                    confidence=0.95,
                    locator=MaterialLocator(page=1, bbox=(10, 20, 100, 30)),
                )
            ],
        )


async def test_low_confidence_is_held_out_of_automatic_evidence() -> None:
    service = MaterialIngestService(
        downloader=MockDownloader(),
        asr=LowConfidenceASR(),
        ocr=MockOCRProvider(),
        document_parser=MockDocumentParser(),
    )
    material = (await service.ingest([audio()]))[0]
    assert material.status is MaterialStatus.manual_review
    assert material.automatic_evidence_use is False
    assert material.automatic_text == ""
    assert material.review_queue == [material.segments[0].segment_id]
    assert material.segments[0].quality_flags == ["low_confidence"]


async def test_one_attachment_failure_does_not_drop_other_materials() -> None:
    service = MaterialIngestService(
        downloader=MockDownloader(),
        asr=FailingASR(),
        ocr=MockOCRProvider(),
        document_parser=MockDocumentParser(),
    )
    failed, succeeded = await service.ingest([audio(), file("观察笔记.docx")])
    assert failed.status is MaterialStatus.failed
    assert failed.issues[0].code == "XDW-MM-UNEXPECTED"
    assert "secret" not in failed.model_dump_json()
    assert succeeded.status is MaterialStatus.ready


async def test_scanned_pdf_falls_back_to_ocr_and_keeps_page_bbox() -> None:
    service = MaterialIngestService(
        downloader=MockDownloader(),
        asr=LowConfidenceASR(),
        ocr=LocatedOCR(),
        document_parser=OCRRequiredParser(),
    )
    material = (await service.ingest([file("扫描材料.pdf")]))[0]
    assert material.status is MaterialStatus.ready
    assert material.modality is MaterialModality.document
    assert material.provider_name == "test-ocr"
    assert material.segments[0].locator.page == 1
    assert material.segments[0].locator.bbox == (10.0, 20.0, 100.0, 30.0)


def live_service(api_key: str) -> MaterialIngestService:
    return build_live_ingest_service(
        max_upload_bytes=1024,
        max_document_chars=1000,
        connect_timeout=1,
        read_timeout=1,
        max_redirects=0,
        asr_provider="stepfun",
        stepfun_asr_api_key=api_key,
        stepfun_asr_base_url="https://api.stepfun.com/v1",
        stepfun_asr_model="step-asr-1.1",
        stepfun_asr_request_timeout=1,
        stepfun_asr_poll_timeout=1,
        stepfun_asr_poll_interval=0,
        ocr_provider="disabled",
    )


def test_live_service_only_enables_audio_when_real_key_is_configured() -> None:
    disabled = live_service("")
    assert disabled.enabled_modalities == frozenset({MaterialModality.document})
    assert isinstance(disabled.asr, UnavailableASRProvider)

    enabled = live_service("test-key")
    assert enabled.enabled_modalities == frozenset(
        {MaterialModality.audio, MaterialModality.document}
    )
    assert isinstance(enabled.asr, StepFunASRProvider)


def test_live_service_only_enables_ocr_when_both_credentials_are_configured() -> None:
    common = {
        "max_upload_bytes": 1024,
        "max_document_chars": 1000,
        "connect_timeout": 1,
        "read_timeout": 1,
        "max_redirects": 0,
        "asr_provider": "disabled",
        "stepfun_asr_api_key": "",
        "stepfun_asr_base_url": "https://api.stepfun.com/v1",
        "stepfun_asr_model": "step-asr-1.1",
        "stepfun_asr_request_timeout": 1,
        "stepfun_asr_poll_timeout": 1,
        "stepfun_asr_poll_interval": 0,
        "ocr_provider": "baidu",
    }
    disabled = build_live_ingest_service(
        **common,
        baidu_ocr_api_key="only-ak",
        baidu_ocr_secret_key="",
    )
    assert MaterialModality.image not in disabled.enabled_modalities
    assert isinstance(disabled.ocr, UnavailableOCRProvider)

    enabled = build_live_ingest_service(
        **common,
        baidu_ocr_api_key="test-ak",
        baidu_ocr_secret_key="test-sk",
    )
    assert MaterialModality.image in enabled.enabled_modalities
    assert isinstance(enabled.ocr, BaiduOCRProvider)
