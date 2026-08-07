"""多模态 Provider 接口和测试实现。"""

from app.multimodal.providers.base import ASRProvider, DocumentParser, OCRProvider
from app.multimodal.providers.mock import MockASRProvider, MockDocumentParser, MockOCRProvider

__all__ = [
    "ASRProvider",
    "DocumentParser",
    "MockASRProvider",
    "MockDocumentParser",
    "MockOCRProvider",
    "OCRProvider",
]
