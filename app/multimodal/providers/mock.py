"""无网络、无密钥、无费用的 v2.2.0 契约测试 Provider。"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.multimodal.contracts import FileContentPart, InputAudioContentPart
from app.multimodal.models import MaterialLocator, ProviderResult, ProviderSegment
from app.multimodal.providers.base import ASRProvider, DocumentParser, OCRProvider


class MockASRProvider(ASRProvider):
    async def transcribe(self, source: InputAudioContentPart) -> ProviderResult:
        return ProviderResult(
            provider_name="mock",
            provider_model="mock-asr-contract-v1",
            segments=[
                ProviderSegment(
                    text="这是 Mock ASR 片段，仅用于验证接口契约。",
                    confidence=0.95,
                    locator=MaterialLocator(start_ms=0, end_ms=2500),
                )
            ],
            warnings=["mock_provider_result"],
        )


class MockOCRProvider(OCRProvider):
    async def recognize(self, source: FileContentPart) -> ProviderResult:
        return ProviderResult(
            provider_name="mock",
            provider_model="mock-ocr-contract-v1",
            segments=[
                ProviderSegment(
                    text="这是 Mock OCR 片段，仅用于验证接口契约。",
                    confidence=0.95,
                    locator=MaterialLocator(page=1, bbox=(100, 100, 600, 80)),
                )
            ],
            warnings=["mock_provider_result"],
        )


class MockDocumentParser(DocumentParser):
    async def parse(self, source: FileContentPart) -> ProviderResult:
        suffix = PurePosixPath(source.file.filename).suffix.lower()
        locator = (
            MaterialLocator(page=1, char_start=0, char_end=24)
            if suffix == ".pdf"
            else MaterialLocator(char_start=0, char_end=24)
        )
        return ProviderResult(
            provider_name="mock",
            provider_model="mock-document-parser-contract-v1",
            segments=[
                ProviderSegment(
                    text="这是 Mock 文档片段，仅用于验证接口契约。",
                    confidence=1.0,
                    locator=locator,
                )
            ],
            warnings=["mock_provider_result"],
        )
