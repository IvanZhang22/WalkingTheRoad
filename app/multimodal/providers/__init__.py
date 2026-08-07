"""多模态 Provider 接口和测试实现。"""

from app.multimodal.providers.baidu_ocr import BaiduOCRProvider
from app.multimodal.providers.base import ASRProvider, DocumentParser, OCRProvider
from app.multimodal.providers.document import LocalDocumentParser
from app.multimodal.providers.mock import MockASRProvider, MockDocumentParser, MockOCRProvider
from app.multimodal.providers.stepfun_asr import StepFunASRProvider
from app.multimodal.providers.unavailable import UnavailableASRProvider, UnavailableOCRProvider

__all__ = [
    "ASRProvider",
    "BaiduOCRProvider",
    "DocumentParser",
    "LocalDocumentParser",
    "MockASRProvider",
    "MockDocumentParser",
    "MockOCRProvider",
    "OCRProvider",
    "StepFunASRProvider",
    "UnavailableASRProvider",
    "UnavailableOCRProvider",
]
