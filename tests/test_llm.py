from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm import LLMError, OpenAICompatibleClient
from app.models import ResearchDiagnosis


class FakeCompletions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.requests: list[dict] = []

    async def create(self, **kwargs):  # type: ignore[no-untyped-def]
        self.requests.append(kwargs)
        content = self.responses[self.calls]
        self.calls += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def settings(api_key: str = "test-key", provider: str = "deepseek") -> Settings:
    return Settings(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking="disabled",
        app_mode="live",
        timeout_seconds=120,
        max_upload_bytes=20 * 1024 * 1024,
        max_document_chars=300_000,
        provider=provider,
    )


@pytest.mark.asyncio
async def test_invalid_json_is_repaired_once() -> None:
    client = OpenAICompatibleClient(settings())
    repaired = """{
      "scope_problems": [], "operationalization": [], "participant_fit": [],
      "method_fit": [], "time_and_resource_risks": [], "known_facts": [],
      "provisional_assumptions": [], "decisions_needed": []
    }"""
    fake = FakeCompletions(["不是JSON", repaired])
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # type: ignore[assignment]
    result = await client.complete(
        node_id="3L-1-1",
        system_prompt="system",
        user_prompt="user",
        temperature=0.1,
        json_model=ResearchDiagnosis,
    )
    assert fake.calls == 2
    assert '"scope_problems"' in result


def test_missing_api_key_fails_clearly() -> None:
    with pytest.raises(LLMError, match="MODEL_API_KEY"):
        OpenAICompatibleClient(settings(api_key=""))


@pytest.mark.asyncio
async def test_stepfun_does_not_receive_deepseek_thinking_parameter() -> None:
    client = OpenAICompatibleClient(
        Settings(
            api_key="step-key",
            base_url="https://api.stepfun.com/step_plan/v1",
            model="step-router-v1",
            thinking="disabled",
            app_mode="live",
            timeout_seconds=120,
            max_upload_bytes=20 * 1024 * 1024,
            max_document_chars=300_000,
            provider="stepfun",
        )
    )
    fake = FakeCompletions(["阶跃测试回答"])
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # type: ignore[assignment]
    await client.complete(
        node_id="3L-1-2",
        system_prompt="system",
        user_prompt="user",
        temperature=0.2,
    )
    assert fake.requests[0]["model"] == "step-router-v1"
    assert "extra_body" not in fake.requests[0]


@pytest.mark.asyncio
async def test_vercel_uses_gateway_json_schema_format() -> None:
    client = OpenAICompatibleClient(
        Settings(
            api_key="oidc-test-token",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-5.4-mini",
            thinking="disabled",
            app_mode="live",
            timeout_seconds=120,
            max_upload_bytes=20 * 1024 * 1024,
            max_document_chars=300_000,
            provider="vercel",
        )
    )
    content = ResearchDiagnosis(
        scope_problems=[],
        operationalization=[],
        participant_fit=[],
        method_fit=[],
        time_and_resource_risks=[],
        known_facts=[],
        provisional_assumptions=[],
        decisions_needed=[],
    ).model_dump_json()
    fake = FakeCompletions([content])
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # type: ignore[assignment]

    await client.complete(
        node_id="3L-1-1",
        system_prompt="system",
        user_prompt="user",
        temperature=0.1,
        json_model=ResearchDiagnosis,
    )

    response_format = fake.requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "xingxiaodao_3L-1-1"
    assert "extra_body" not in fake.requests[0]
