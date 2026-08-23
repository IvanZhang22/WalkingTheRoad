from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import MockLLMClient
from app.main import create_app
from app.multimodal.downloader import MockDownloader
from app.multimodal.models import DownloadedFile, MaterialLocator, ProviderResult, ProviderSegment
from app.multimodal.providers.base import ASRProvider
from app.multimodal.providers.mock import MockDocumentParser, MockOCRProvider
from app.multimodal.service import MaterialIngestService


class StepFunShapeASR(ASRProvider):
    """Represents a StepFun ASR response with timestamps but no confidence."""

    async def transcribe(self, source: DownloadedFile) -> ProviderResult:
        return ProviderResult(
            provider_name="stepfun",
            provider_model="step-asr-1.1",
            normalized_text="Interviewee says the employment-service entry point is hard to find.",
            segments=[
                ProviderSegment(
                    text="Interviewee says the employment-service entry point is hard to find.",
                    confidence=None,
                    locator=MaterialLocator(start_ms=500, end_ms=2800),
                )
            ],
            warnings=["Upstream response did not provide confidence."],
        )


def _settings() -> Settings:
    return Settings(
        api_key="",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking="disabled",
        app_mode="mock",
        timeout_seconds=120,
        max_upload_bytes=20 * 1024 * 1024,
        max_document_chars=300_000,
        agent_api_key="test-agent-key",
    )


def _chat(client: TestClient, session_id: str, text: str) -> str:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-agent-key"},
        json={"sessionId": session_id, "messages": [{"role": "user", "content": text}]},
    )
    assert response.status_code == 200
    return response.json()["choices"][0]["message"]["content"]


def test_missing_confidence_transcript_requires_manual_confirmation() -> None:
    ingest = MaterialIngestService(
        downloader=MockDownloader(),
        asr=StepFunShapeASR(),
        ocr=MockOCRProvider(),
        document_parser=MockDocumentParser(),
    )
    app = create_app(settings=_settings(), llm=MockLLMClient(), material_ingestor=ingest)
    session_id = "stepfun-manual-review"

    with TestClient(app) as client:
        _chat(client, session_id, "3")
        _chat(client, session_id, "How do students understand access to employment services?")
        _chat(client, session_id, "Interview recording A")
        _chat(client, session_id, "1")
        _chat(client, session_id, "skip")
        _chat(client, session_id, "1")
        uploaded = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-agent-key"},
            json={
                "sessionId": session_id,
                "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"url": "https://files.example.org/interview.mp3", "format": "mp3"}}]}],
            },
        )
        assert uploaded.status_code == 200
        review = _chat(client, session_id, "1")
        assert "置信度" in review
        assert "employment-service entry point" in review
        assert app.state.conversation.store.get(session_id).step == "manual_review"
        _chat(client, session_id, "1")
        saved = app.state.conversation.store.get(session_id)
        assert saved.step == "result_review"
        assert saved.field_values()["__manual_review_confirmed"] is True
