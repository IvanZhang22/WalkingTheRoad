"""多模态材料接入的内部契约与 Provider 边界。"""

from app.multimodal.service import (
    MaterialIngestService,
    build_document_ingest_service,
    build_live_ingest_service,
    build_mock_ingest_service,
)

__all__ = [
    "MaterialIngestService",
    "build_document_ingest_service",
    "build_live_ingest_service",
    "build_mock_ingest_service",
]
