"""多模态接入层可安全返回给调用方的结构化错误。"""

from __future__ import annotations


class MaterialIngestError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.retryable = retryable
