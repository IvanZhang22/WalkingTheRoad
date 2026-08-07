from __future__ import annotations

from app.multimodal.contracts import FileContentPart, InputAudioContentPart
from app.multimodal.models import (
    MaterialLocator,
    MaterialStatus,
    ProviderResult,
    ProviderSegment,
)
from app.multimodal.providers.base import ASRProvider
from app.multimodal.providers.mock import MockDocumentParser, MockOCRProvider
from app.multimodal.service import MaterialIngestService, build_mock_ingest_service


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
    first = (
        await service.ingest([audio("https://files.example.org/interview.mp3?token=first")])
    )[0]
    second = (
        await service.ingest([audio("https://files.example.org/interview.mp3?token=second")])
    )[0]
    assert first.material_id == second.material_id
    assert first.source_fingerprint == second.source_fingerprint


class LowConfidenceASR(ASRProvider):
    async def transcribe(self, source: InputAudioContentPart) -> ProviderResult:
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
    async def transcribe(self, source: InputAudioContentPart) -> ProviderResult:
        raise RuntimeError("secret upstream detail")


async def test_low_confidence_is_held_out_of_automatic_evidence() -> None:
    service = MaterialIngestService(
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
        asr=FailingASR(),
        ocr=MockOCRProvider(),
        document_parser=MockDocumentParser(),
    )
    failed, succeeded = await service.ingest([audio(), file("观察笔记.docx")])
    assert failed.status is MaterialStatus.failed
    assert failed.issues[0].code == "XDW-MM-UNEXPECTED"
    assert "secret" not in failed.model_dump_json()
    assert succeeded.status is MaterialStatus.ready
