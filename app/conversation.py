"""清小搭对话编排层。

这个模块只负责把平台中的自然语言会话变成可执行的研究步骤；研究
方法生成、证据核验仍由 ``WorkflowService`` 完成。会话状态只保存用户
确认输入的简短字段，不保存附件下载地址或模型提示词。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from app.models import RunStatus
from app.multimodal.contracts import FileContentPart, InputAudioContentPart
from app.multimodal.models import Material

MAIN_MENU = """我可以陪你把社会实践做成一项可靠、可复核的研究。请选择你现在最需要的帮助：

1. 研究设计：把实践想法收敛为可研究的问题和可执行方案
2. 访谈设计：生成访谈提纲，或检查现有问题的引导与伦理风险
3. 材料分析：对访谈、观察笔记或上传材料做编码、主题与引文核验
4. 结论质检：检查一条研究结论的证据、反例、样本边界与推断风险

请回复数字 1、2、3 或 4。任何时候都可以回复“主菜单”“上一步”或“结束”。"""

WORKFLOW_TITLES = {
    "w1": "研究设计",
    "w2": "访谈设计",
    "w3": "材料分析",
    "w4": "结论质检",
}


class WorkflowRunner(Protocol):
    store: Any

    async def execute(
        self,
        run_id: str,
        workflow_id: str,
        raw_fields: dict[str, Any],
        filename: str | None,
        file_bytes: bytes | None,
        project_context: Any = None,
    ) -> None: ...


class MaterialIngestor(Protocol):
    async def ingest(
        self, attachments: list[InputAudioContentPart | FileContentPart]
    ) -> list[Material]: ...


@dataclass(frozen=True)
class ConversationState:
    session_id: str
    workflow_id: str | None = None
    step: str = "menu"
    fields: dict[str, str] | None = None

    def field_values(self) -> dict[str, str]:
        return dict(self.fields or {})


class ConversationStore:
    """很小的 SQLite 状态库；可跨 Uvicorn 重启保存同一会话的进度。"""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    session_id TEXT PRIMARY KEY,
                    workflow_id TEXT,
                    step TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get(self, session_id: str) -> ConversationState:
        with self._lock:
            row = self._connection.execute(
                "SELECT workflow_id, step, fields_json FROM conversation_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return ConversationState(session_id=session_id)
        return ConversationState(
            session_id=session_id,
            workflow_id=row[0],
            step=row[1],
            fields=json.loads(row[2]),
        )

    def save(self, state: ConversationState) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO conversation_state(session_id, workflow_id, step, fields_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    step=excluded.step,
                    fields_json=excluded.fields_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    state.session_id,
                    state.workflow_id,
                    state.step,
                    json.dumps(state.field_values(), ensure_ascii=False),
                ),
            )

    def clear(self, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM conversation_state WHERE session_id = ?", (session_id,)
            )


class QingxiaodaConversation:
    def __init__(
        self,
        *,
        database_path: Path,
        workflow_service: WorkflowRunner,
        material_ingestor: MaterialIngestor | None,
    ) -> None:
        self.store = ConversationStore(database_path)
        self.workflow_service = workflow_service
        self.material_ingestor = material_ingestor

    async def reply(
        self,
        *,
        session_id: str | None,
        text: str,
        attachments: list[InputAudioContentPart | FileContentPart],
    ) -> str:
        # 清小搭正式聊天会提供 sessionId。无 sessionId 的普通协议调用仍可
        # 获得菜单，但不会把状态误共享给其他调用者。
        safe_session_id = (session_id or f"temporary-{uuid4().hex}")[:200]
        state = self.store.get(safe_session_id)
        message = text.strip()

        if message in {"主菜单", "菜单", "0", "重新开始"}:
            self.store.clear(safe_session_id)
            return MAIN_MENU
        if message in {"结束", "退出"}:
            self.store.clear(safe_session_id)
            return "本次会话已结束。之后需要继续研究时，直接发送消息即可重新开始。"
        if message in {"上一步", "返回"}:
            return self._go_back(state)

        if state.workflow_id is None:
            return await self._choose_workflow(state, message, attachments)
        return await self._continue_workflow(state, message, attachments)

    async def _choose_workflow(
        self,
        state: ConversationState,
        message: str,
        attachments: list[InputAudioContentPart | FileContentPart],
    ) -> str:
        selection = self._selection(message)
        if selection is None and attachments:
            selection = "w3"
        if selection is None:
            # 首条自然语言不再向用户显示内部“推荐工作流”，而是给出可确认的
            # 入口。避免模型在没有表单的情况下把责任丢回给用户。
            guessed = self._guess_workflow(message)
            if guessed:
                return (
                    f"我理解你想先做“{WORKFLOW_TITLES[guessed]}”。"
                    f"回复“{self._number_for(guessed)}”开始，或回复其他数字改选：\n\n{MAIN_MENU}"
                )
            return MAIN_MENU
        started = self._start_workflow(state, selection)
        if attachments:
            return (
                "我看到了你已上传的材料。为保证分析目标清楚，请先补充下面的问题；"
                "最后一步我会请你重新上传材料并开始处理。\n\n" + started
            )
        return started

    def _start_workflow(self, state: ConversationState, workflow_id: str) -> str:
        fields: dict[str, str] = {}
        if workflow_id == "w1":
            next_state = ConversationState(state.session_id, "w1", "theme", fields)
            prompt = "好的，我们先梳理研究设计。\n\n第一步：请用一句话描述你想研究的社会实践主题。"
        elif workflow_id == "w2":
            next_state = ConversationState(state.session_id, "w2", "mode", fields)
            prompt = "好的，开始访谈设计。请选择：\n\n1. 从零生成一份访谈提纲\n2. 审查我已有的访谈问题\n\n请回复 1 或 2。"
        elif workflow_id == "w3":
            next_state = ConversationState(state.session_id, "w3", "research_question", fields)
            prompt = "好的，开始材料分析。\n\n第一步：这批材料要回答的研究问题是什么？"
        else:
            next_state = ConversationState(state.session_id, "w4", "research_question", fields)
            prompt = "好的，开始结论质检。\n\n第一步：请写出这项研究希望回答的研究问题。"
        self.store.save(next_state)
        return prompt

    async def _continue_workflow(
        self,
        state: ConversationState,
        message: str,
        attachments: list[InputAudioContentPart | FileContentPart],
    ) -> str:
        workflow_id = state.workflow_id
        assert workflow_id is not None
        fields = state.field_values()

        if workflow_id == "w2" and state.step == "mode":
            if message not in {"1", "2"}:
                return "请回复 1（从零生成）或 2（审查已有问题）。"
            fields["mode"] = "generate" if message == "1" else "review"
            step = "research_question" if message == "1" else "review_topic"
            self.store.save(ConversationState(state.session_id, workflow_id, step, fields))
            return (
                "请写出你想回答的研究问题。"
                if message == "1"
                else "请写出这组问题对应的研究主题或研究问题。"
            )

        if workflow_id == "w3" and state.step == "source_type":
            material_types = {
                "1": "单份访谈",
                "2": "多份访谈",
                "3": "田野或观察笔记",
                "4": "混合材料",
            }
            if message not in material_types:
                return "请回复 1、2、3 或 4 选择材料类型。"
            fields["source_type"] = material_types[message]
            next_step, next_prompt = self._next_step(workflow_id, state.step, fields)
            assert next_step is not None
            self.store.save(ConversationState(state.session_id, workflow_id, next_step, fields))
            return next_prompt

        if state.step == "upload":
            if not attachments:
                return "请使用清小搭输入框左下角的“上传文件”发送原始材料；发送后我会开始分析。"
            return await self._run_with_attachments(state, attachments)

        if not message:
            return "请补充这一项信息；也可以回复“上一步”修改前一项。"
        if message == "跳过" and state.step not in self._required_steps(workflow_id, fields):
            message = ""
        elif message == "跳过":
            return "这一项是完成当前任务所必需的信息，请用自己的话补充。"
        fields[state.step] = message
        next_step, next_prompt = self._next_step(workflow_id, state.step, fields)
        if next_step is not None:
            self.store.save(ConversationState(state.session_id, workflow_id, next_step, fields))
            return next_prompt
        return await self._run_without_attachments(state.session_id, workflow_id, fields)

    async def _run_with_attachments(
        self,
        state: ConversationState,
        attachments: list[InputAudioContentPart | FileContentPart],
    ) -> str:
        if self.material_ingestor is None:
            return "文件已收到，但材料处理服务暂不可用。请稍后重试，或先发送文字材料。"
        materials = await self.material_ingestor.ingest(attachments)
        usable = [
            item
            for item in materials
            if item.automatic_text.strip() or item.normalized_text.strip()
        ]
        if not usable:
            issues = (
                "；".join(issue.message for item in materials for issue in item.issues[:1])
                or "没有提取到可用文本"
            )
            return f"我暂时无法从这份材料中提取可核验文本：{issues}。请更换文件或补充可复制的文字材料。"
        text = "\n\n".join(
            f"【{item.filename}】\n{item.normalized_text or item.automatic_text}" for item in usable
        )
        return await self._run_workflow(
            state.session_id,
            state.workflow_id or "w3",
            state.field_values(),
            filename="qingxiaoda-material.txt",
            file_bytes=text.encode("utf-8"),
        )

    async def _run_without_attachments(
        self, session_id: str, workflow_id: str, fields: dict[str, str]
    ) -> str:
        if workflow_id in {"w3", "w4"}:
            self.store.save(ConversationState(session_id, workflow_id, "upload", fields))
            return (
                "请上传原始材料后继续。支持文档、音频与图片；材料会先被转为可核验文本，再开始分析。"
            )
        return await self._run_workflow(session_id, workflow_id, fields, None, None)

    async def _run_workflow(
        self,
        session_id: str,
        workflow_id: str,
        fields: dict[str, str],
        filename: str | None,
        file_bytes: bytes | None,
    ) -> str:
        record = await self.workflow_service.store.create(workflow_id)
        await self.workflow_service.execute(
            record.run_id, workflow_id, fields, filename, file_bytes, None
        )
        completed = await self.workflow_service.store.get(record.run_id)
        self.store.clear(session_id)
        if completed is None or completed.status != RunStatus.succeeded:
            return "这次处理没有完成。请检查输入或材料后重试；也可以回复“主菜单”选择其他任务。"
        title = WORKFLOW_TITLES[workflow_id]
        return (
            f"## {title}结果\n\n{completed.final_markdown or ''}\n\n"
            "---\n回复“主菜单”开始另一项任务；回复“结束”关闭本次会话。"
        )

    def _go_back(self, state: ConversationState) -> str:
        if state.workflow_id is None:
            return MAIN_MENU
        fields = state.field_values()
        order = self._field_order(state.workflow_id, fields)
        if state.step == "mode":
            return self._start_workflow(ConversationState(state.session_id), state.workflow_id)
        if state.step == "upload":
            previous = order[-1]
        else:
            try:
                index = order.index(state.step)
                previous = order[max(0, index - 1)]
            except ValueError:
                previous = order[0]
        fields.pop(previous, None)
        self.store.save(ConversationState(state.session_id, state.workflow_id, previous, fields))
        _, prompt = self._next_step(
            state.workflow_id, self._previous_step(state.workflow_id, previous, fields), fields
        )
        return f"好的，请重新填写：{prompt}"

    @staticmethod
    def _selection(message: str) -> str | None:
        return {"1": "w1", "2": "w2", "3": "w3", "4": "w4"}.get(message.strip())

    @staticmethod
    def _number_for(workflow_id: str) -> str:
        return {"w1": "1", "w2": "2", "w3": "3", "w4": "4"}[workflow_id]

    @staticmethod
    def _guess_workflow(message: str) -> str | None:
        text = message.lower()
        if any(word in text for word in ("编码", "主题", "访谈记录", "田野", "材料分析")):
            return "w3"
        if any(word in text for word in ("质检", "核验", "结论", "证据", "反例")):
            return "w4"
        if any(word in text for word in ("访谈", "提纲", "问题清单")):
            return "w2"
        if any(word in text for word in ("选题", "研究问题", "研究设计", "社会实践")):
            return "w1"
        return None

    @staticmethod
    def _field_order(workflow_id: str, fields: dict[str, str]) -> list[str]:
        if workflow_id == "w1":
            return ["theme", "purpose", "background", "deadline", "participants", "resources"]
        if workflow_id == "w2" and fields.get("mode") == "review":
            return [
                "review_topic",
                "existing_questions",
                "review_participant",
                "review_requirements",
            ]
        if workflow_id == "w2":
            return ["research_question", "participant_profile", "duration", "sensitive_topics"]
        if workflow_id == "w3":
            return ["research_question", "source_id", "source_type", "source_context"]
        return [
            "research_question",
            "candidate_claim",
            "target_population",
            "sample_summary",
            "source_id",
            "source_context",
        ]

    def _next_step(
        self, workflow_id: str, current: str, fields: dict[str, str]
    ) -> tuple[str | None, str]:
        order = self._field_order(workflow_id, fields)
        try:
            following = order[order.index(current) + 1]
        except (ValueError, IndexError):
            return None, ""
        prompts = {
            "purpose": "第二步：你希望通过这项研究理解、解释或改进什么？",
            "background": "请补充已知背景或你的初步判断；没有可回复“跳过”。",
            "deadline": "这项实践或研究的时间限制是什么？没有可回复“跳过”。",
            "participants": "你目前可以接触哪些对象或场景？没有可回复“跳过”。",
            "resources": "团队人数、可用资源或重要限制是什么？没有可回复“跳过”。",
            "participant_profile": "计划访谈谁？请描述对象范围或筛选条件。",
            "duration": "每次访谈预计多长时间？",
            "sensitive_topics": "有哪些敏感主题、伦理边界或访谈限制？没有可回复“跳过”。",
            "existing_questions": "请直接粘贴需要审查的访谈问题。",
            "review_participant": "这组问题准备问谁？没有可回复“跳过”。",
            "review_requirements": "还有什么特殊要求？没有可回复“跳过”。",
            "source_id": "请给这批材料取一个便于引用的名称或编号，例如“访谈材料包 A”。",
            "source_type": "材料类型请选择：\n1. 单份访谈\n2. 多份访谈\n3. 田野或观察笔记\n4. 混合材料\n\n请回复数字。",
            "source_context": "请补充采集场景、对象范围、日期或材料限制；没有可回复“跳过”。",
            "candidate_claim": "请写出需要核验的一条结论或判断。",
            "target_population": "这条结论原本想讨论的目标群体是谁？",
            "sample_summary": "实际样本是什么情况？请写人数、来源和关键特征；未知信息请明确写“未知”。",
        }
        return following, prompts[following]

    @staticmethod
    def _required_steps(workflow_id: str, fields: dict[str, str]) -> set[str]:
        if workflow_id == "w1":
            return {"theme", "purpose"}
        if workflow_id == "w2" and fields.get("mode") == "review":
            return {"review_topic", "existing_questions"}
        if workflow_id == "w2":
            return {"research_question", "participant_profile", "duration"}
        if workflow_id == "w3":
            return {"research_question", "source_id", "source_type"}
        return {
            "research_question",
            "candidate_claim",
            "target_population",
            "sample_summary",
            "source_id",
        }

    @staticmethod
    def _previous_step(workflow_id: str, target: str, fields: dict[str, str]) -> str:
        order = QingxiaodaConversation._field_order(workflow_id, fields)
        index = order.index(target)
        return order[max(0, index - 1)]
