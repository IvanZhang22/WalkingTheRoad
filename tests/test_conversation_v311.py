from __future__ import annotations

from fastapi.testclient import TestClient

from app.conversation import ConversationState
from app.llm import MockLLMClient
from app.main import create_app
from tests.test_openai_compat import headers, settings


def _chat(client: TestClient, session_id: str, text: str) -> str:
    response = client.post(
        "/v1/chat/completions",
        headers=headers(),
        json={
            "sessionId": session_id,
            "messages": [{"role": "user", "content": text}],
        },
    )
    assert response.status_code == 200
    return response.json()["choices"][0]["message"]["content"]


def test_current_menu_owns_two_consecutive_numeric_answers() -> None:
    """The second ``1`` is interpreted by the after-confirm menu, not result review."""

    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        session_id = "v311-current-menu"
        _chat(client, session_id, "1")
        _chat(client, session_id, "暑期支教学生的公共服务体验")
        _chat(client, session_id, "理解学生如何理解公共服务责任")
        _chat(client, session_id, "跳过")
        _chat(client, session_id, "跳过")
        _chat(client, session_id, "支教学生和带队老师")
        assert "待你确认" in _chat(client, session_id, "4 人小组，实践期两周")
        assert "由你确认采用" in _chat(client, session_id, "1")
        # Same number, but now it means continue to interview design.
        assert "访谈" in _chat(client, session_id, "1")
        # Same number again belongs to the interview-mode menu.
        assert "研究问题" in _chat(client, session_id, "1")


def test_natural_language_replaces_a_pending_recommendation() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        session_id = "v311-natural-override"
        assert "访谈" in _chat(client, session_id, "帮我设计访谈提纲")
        # The previous screen is interview mode, but the explicit new intent wins.
        reply = _chat(client, session_id, "不分析访谈了，我想先分析已有材料")
        assert "材料分析" in reply
        assert "回复 **1**" in reply


def test_natural_language_answers_before_optional_workflow_handoff() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        session_id = "v320-natural-handoff"
        reply = _chat(client, session_id, "帮我设计一份给支教学生的访谈提纲")
        assert "访谈" in reply
        assert "## 一、" in reply
        assert "## 二、" in reply
        assert "回复 **1**" in reply
        assert client.app.state.conversation.store.get(session_id).workflow_id is None

        started = _chat(client, session_id, "1")
        assert "访谈" in started
        assert client.app.state.conversation.store.get(session_id).workflow_id == "w2"


def test_project_context_conflict_requires_researcher_confirmation() -> None:
    app = create_app(settings=settings(), llm=MockLLMClient())
    with TestClient(app) as client:
        session_id = "v311-context-conflict"
        conversation = app.state.conversation
        state = conversation.store.get(session_id)
        project = state.project_values()
        project["research_question"] = "返乡青年如何理解县域就业机会？"
        conversation.store.save(
            ConversationState(
                session_id,
                "w2",
                "research_question",
                {},
                project,
            )
        )
        conflict = _chat(client, session_id, "返乡青年为什么选择自主创业？")
        assert "不一致" in conflict
        assert "替换" in conflict
        follow_up = _chat(client, session_id, "保留当前内容")
        assert "计划访谈谁" in follow_up
        saved = conversation.store.get(session_id).project_values()
        assert saved["research_question"] == "返乡青年如何理解县域就业机会？"


def test_named_projects_are_isolated_and_archives_can_be_restored() -> None:
    with TestClient(create_app(settings=settings(), llm=MockLLMClient())) as client:
        session_id = "v311-projects"
        assert "已新建项目“河南返乡青年”" in _chat(client, session_id, "新建项目：河南返乡青年")
        _chat(client, session_id, "1")
        _chat(client, session_id, "河南县域返乡青年创业")
        assert "已知背景" in _chat(client, session_id, "理解创业选择的家庭与地方条件")
        assert "已新建项目“暑期支教”" in _chat(client, session_id, "新建项目：暑期支教")
        overview = _chat(client, session_id, "项目列表")
        assert "河南返乡青年" in overview and "暑期支教（当前）" in overview
        switched = _chat(client, session_id, "切换项目：河南返乡青年")
        assert "河南县域返乡青年创业" in switched
        assert "确认归档" in _chat(client, session_id, "归档项目：河南返乡青年")
        assert "已归档" in _chat(client, session_id, "确认归档")
        assert "河南返乡青年" in _chat(client, session_id, "查看归档项目")
        restored = _chat(client, session_id, "恢复项目：河南返乡青年")
        assert "已恢复并切换" in restored
