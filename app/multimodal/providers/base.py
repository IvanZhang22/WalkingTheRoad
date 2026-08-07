"""厂商无关的异步 ASR、OCR 与文档解析接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.multimodal.models import DownloadedFile, ProviderResult


class ASRProvider(ABC):
    @abstractmethod
    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        """把音频转换成带时间戳的统一片段。"""


class OCRProvider(ABC):
    @abstractmethod
    async def recognize(self, source: DownloadedFile) -> ProviderResult:
        """把图片或扫描页转换成带页码/bbox 的统一片段。"""


class DocumentParser(ABC):
    @abstractmethod
    async def parse(self, source: DownloadedFile) -> ProviderResult:
        """把普通文档转换成带字符区间或页码的统一片段。"""
