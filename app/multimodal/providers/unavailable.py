"""Live 环境尚未启用的 Provider，始终失败关闭。"""

from __future__ import annotations

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile, ProviderResult
from app.multimodal.providers.base import ASRProvider, OCRProvider


class UnavailableASRProvider(ASRProvider):
    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        raise MaterialIngestError(
            "XDW-ASR-NOT-CONFIGURED", "真实音频 ASR Provider 尚未配置。"
        )


class UnavailableOCRProvider(OCRProvider):
    async def recognize(self, source: DownloadedFile) -> ProviderResult:
        raise MaterialIngestError(
            "XDW-OCR-NOT-CONFIGURED", "真实图片 OCR Provider 尚未配置。"
        )
