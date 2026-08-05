import json
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import MockLLMClient
from app.main import create_app


def settings() -> Settings:
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


def test_health_workflow_run_and_markdown_download() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        assert client.get("/api/health").status_code == 200
        assert len(client.get("/api/workflows").json()) == 4
        response = client.post(
            "/api/runs",
            data={
                "workflow_id": "w1",
                "fields_json": '{"theme":"社区服务","purpose":"理解使用障碍"}',
            },
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(50):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert run["status"] == "succeeded"
        exported = client.get(f"/api/runs/{run_id}/download.md")
        assert exported.status_code == 200
        assert exported.text == run["final_markdown"]


def test_intent_route_recommends_without_creating_a_run() -> None:
    app = create_app(settings=settings(), llm=MockLLMClient())
    with TestClient(app) as client:
        response = client.post(
            "/api/route",
            json={"message": "我有三份访谈记录，想做主题分析"},
        )
        assert response.status_code == 200
        assert response.json()["recommended_workflow"] == "w3"
        assert app.state.store._runs == {}


def test_intent_route_rejects_empty_or_extra_input() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        assert client.post("/api/route", json={"message": "   "}).status_code == 422
        assert client.post(
            "/api/route", json={"message": "研究设计", "workflow_id": "w1"}
        ).status_code == 422


def test_intent_route_returns_503_when_model_is_unavailable() -> None:
    app = create_app(settings=settings(), llm=MockLLMClient())
    app.state.llm = None
    app.state.llm_error = "测试配置错误"
    with TestClient(app) as client:
        response = client.post("/api/route", json={"message": "研究设计"})
        assert response.status_code == 503
        assert "测试配置错误" in response.json()["detail"]


def test_run_accepts_whitelisted_project_context_and_returns_patch() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        context = {
            "schema_version": 1,
            "project_id": "P-test",
            "project_name": "测试项目",
            "revision": 2,
            "research_question": "哪些因素影响参与？",
        }
        response = client.post(
            "/api/runs",
            data={
                "workflow_id": "w1",
                "fields_json": '{"theme":"社区服务","purpose":"理解使用障碍"}',
                "project_context_json": json.dumps(context, ensure_ascii=False),
            },
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(50):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert run["status"] == "succeeded"
        assert run["proposed_project_patch"]["workflow_id"] == "w1"


def test_run_rejects_project_context_with_raw_text() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        response = client.post(
            "/api/runs",
            data={
                "workflow_id": "w1",
                "fields_json": '{"theme":"社区服务","purpose":"理解使用障碍"}',
                "project_context_json": '{"project_id":"P-1","project_name":"测试","source_text":"原文"}',
            },
        )
        assert response.status_code == 400
        assert "项目卡上下文无效" in response.json()["detail"]
