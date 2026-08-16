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

MAIN_MENU = """你好，我是行小道，你的社会实践与社会科学调研智能助手。

我可以陪你完成研究设计、访谈设计、材料分析、证据质检与补访建议。你可以直接用自己的话告诉我遇到了什么问题，也可以选择一个任务：

1. 把一个想法变成研究方案
2. 设计或检查访谈
3. 整理和分析已有材料
4. 检查研究结论是否站得住

行小道不会虚构访谈、编造数据或原始引文，也不会把有限质性样本包装成总体结论。AI 的建议需要由你结合原始材料和导师意见确认。

请回复 1、2、3 或 4；不知道选哪个，就直接说说你现在做到哪一步。任何时候可回复“主菜单”“上一步”或“结束”。"""

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
    fields: dict[str, Any] | None = None
    project: dict[str, Any] | None = None

    def field_values(self) -> dict[str, Any]:
        return dict(self.fields or {})

    def project_values(self) -> dict[str, Any]:
        project = dict(self.project or {})
        project.setdefault("workflow_status", {})
        project.setdefault("results", {})
        return project


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
                    project_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(conversation_state)")
            }
            if "project_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE conversation_state ADD COLUMN project_json TEXT NOT NULL DEFAULT '{}'"
                )

    def get(self, session_id: str) -> ConversationState:
        with self._lock:
            row = self._connection.execute(
                "SELECT workflow_id, step, fields_json, project_json FROM conversation_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return ConversationState(session_id=session_id)
        return ConversationState(
            session_id=session_id,
            workflow_id=row[0],
            step=row[1],
            fields=json.loads(row[2]),
            project=json.loads(row[3]),
        )

    def save(self, state: ConversationState) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO conversation_state(session_id, workflow_id, step, fields_json, project_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    step=excluded.step,
                    fields_json=excluded.fields_json,
                    project_json=excluded.project_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    state.session_id,
                    state.workflow_id,
                    state.step,
                    json.dumps(state.field_values(), ensure_ascii=False),
                    json.dumps(state.project_values(), ensure_ascii=False),
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

        if message in {"主菜单", "菜单", "0", "重新开始", "重新选择任务"}:
            menu_state = ConversationState(
                safe_session_id, fields={}, project=state.project_values()
            )
            self.store.save(menu_state)
            return self._menu_for(menu_state.project_values())
        if message in {"结束", "退出", "算了", "不做了", "先到这里"}:
            self.store.save(
                ConversationState(safe_session_id, fields={}, project=state.project_values())
            )
            return self._ending_for(state.project_values())
        if message in {"上一步", "返回"}:
            return self._go_back(state)
        safety_reply = self._safety_reply(message)
        if safety_reply:
            return safety_reply

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
            candidates = self._candidate_workflows(message)
            if len(candidates) == 1:
                guessed = candidates[0]
                return (
                    f"根据你的描述，我建议先做“{WORKFLOW_TITLES[guessed]}”。\n"
                    f"原因：{self._route_reason(guessed)}\n\n"
                    f"回复“{self._number_for(guessed)}”进入；也可以回复其他数字自行选择。\n\n"
                    f"{self._menu_for(state.project_values())}"
                )
            if len(candidates) == 2:
                first, second = candidates
                return (
                    "你现在可能处在两个相邻阶段：\n\n"
                    f"{self._number_for(first)}. {WORKFLOW_TITLES[first]}——{self._route_reason(first)}\n"
                    f"{self._number_for(second)}. {WORKFLOW_TITLES[second]}——{self._route_reason(second)}\n\n"
                    "请回复数字选择。你始终可以自行纠正我的建议。"
                )
            return (
                "我还不能准确判断哪一步最适合你。哪一种最接近你的情况？\n\n"
                "1. 只有一个想法，还不知道具体研究什么\n"
                "2. 已有研究问题，准备开始访谈\n"
                "3. 已有访谈或田野材料，需要分析\n"
                "4. 已形成一些结论，希望检查是否可靠"
            )
        started = self._start_workflow(state, selection)
        if attachments:
            return (
                "我看到了你已上传的材料。为保证分析目标清楚，请先补充下面的问题；"
                "最后一步我会请你重新上传材料并开始处理。\n\n" + started
            )
        return started

    def _start_workflow(self, state: ConversationState, workflow_id: str) -> str:
        fields: dict[str, Any] = {}
        project = state.project_values()
        statuses = dict(project.get("workflow_status", {}))
        statuses[workflow_id] = "IN_PROGRESS"
        project["workflow_status"] = statuses
        if workflow_id == "w1":
            next_state = ConversationState(state.session_id, "w1", "theme", fields, project)
            prompt = "好的，我们先梳理研究设计。\n\n第一步：请用一句话描述你想研究的社会实践主题。"
        elif workflow_id == "w2":
            next_state = ConversationState(state.session_id, "w2", "mode", fields, project)
            prompt = "好的，开始访谈设计。请选择：\n\n1. 从零生成一份访谈提纲\n2. 审查我已有的访谈问题\n\n请回复 1 或 2。"
        elif workflow_id == "w3":
            next_state = ConversationState(
                state.session_id, "w3", "research_question", fields, project
            )
            prompt = "好的，开始材料分析。\n\n第一步：这批材料要回答的研究问题是什么？"
        else:
            next_state = ConversationState(
                state.session_id, "w4", "research_question", fields, project
            )
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
        project = state.project_values()

        if state.step == "result_review":
            return await self._review_result(state, message)
        if state.step == "result_modify":
            if not message:
                return "请直接说明希望改哪一部分；也可以回复“上一步”回到结果选择。"
            target = self._revision_field(workflow_id)
            existing = str(fields.get(target, ""))
            fields[target] = f"{existing}\n\n用户希望调整：{message}".strip()
            return await self._rerun_from_state(state.session_id, workflow_id, fields, project)
        if state.step == "retry":
            if message in {"重新运行当前步骤", "重新运行", "1"}:
                return await self._rerun_from_state(state.session_id, workflow_id, fields, project)
            return "回复“重新运行当前步骤”再次尝试，回复“上一步”修改输入，或回复“主菜单”切换任务。"
        if state.step == "after_confirm":
            return self._after_confirmation(state, message)
        if state.step == "privacy_consent":
            return self._handle_privacy_consent(state, message)
        if state.step == "privacy_help":
            if message == "1":
                self.store.save(
                    ConversationState(state.session_id, workflow_id, "upload", fields, project)
                )
                return self._upload_prompt()
            if message == "2":
                self.store.save(ConversationState(state.session_id, fields={}, project=project))
                return self._menu_for(project)
            return "回复 1 表示已完成匿名化并继续上传；回复 2 返回主菜单。"
        if state.step == "material_confirm":
            return await self._confirm_materials(state, message)
        if state.step == "material_reclassify":
            if not message:
                return "请说明需要如何调整材料分类，例如“政策材料仅作背景，不用于经验结论”。"
            fields["source_context"] = (
                f"{fields.get('source_context', '')}\n材料分类说明：{message}".strip()
            )
            self.store.save(
                ConversationState(
                    state.session_id, workflow_id, "material_confirm", fields, project
                )
            )
            return self._material_confirmation_prompt(fields)
        if state.step == "low_confidence":
            return await self._handle_low_confidence(state, message)

        if workflow_id == "w2" and state.step == "mode":
            if message not in {"1", "2"}:
                return "请回复 1（从零生成）或 2（审查已有问题）。"
            fields["mode"] = "generate" if message == "1" else "review"
            step = "research_question" if message == "1" else "review_topic"
            self.store.save(ConversationState(state.session_id, workflow_id, step, fields, project))
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
            self.store.save(
                ConversationState(state.session_id, workflow_id, next_step, fields, project)
            )
            return next_prompt

        if state.step == "upload":
            if not attachments:
                return self._upload_prompt()
            return await self._receive_attachments(state, attachments)

        if not message:
            return "请补充这一项信息；也可以回复“上一步”修改前一项。"
        if message == "跳过" and state.step not in self._required_steps(workflow_id, fields):
            message = ""
        elif message == "跳过":
            return "这一项是完成当前任务所必需的信息，请用自己的话补充。"
        fields[state.step] = message
        next_step, next_prompt = self._next_step(workflow_id, state.step, fields)
        if next_step is not None:
            self.store.save(
                ConversationState(state.session_id, workflow_id, next_step, fields, project)
            )
            return next_prompt
        return await self._run_without_attachments(state.session_id, workflow_id, fields, project)

    async def _receive_attachments(
        self,
        state: ConversationState,
        attachments: list[InputAudioContentPart | FileContentPart],
    ) -> str:
        if self.material_ingestor is None:
            return self._failure_reply(
                "材料处理服务暂不可用。",
                "你之前填写的研究信息仍已保留，尚未开始分析。",
                "请稍后重试，或先发送可复制的文字材料。",
            )
        try:
            materials = await self.material_ingestor.ingest(attachments)
        except Exception:
            return self._failure_reply(
                "材料读取没有完成。",
                "你之前填写的研究信息没有丢失，尚未形成任何研究结论。",
                "请检查文件格式和网络后重新上传。",
            )
        reliable_chunks: list[str] = []
        names: list[str] = []
        low_confidence: list[str] = []
        for item in materials:
            names.append(item.filename)
            segments = [segment.text for segment in item.segments if segment.automatic_evidence_use]
            if segments:
                reliable_chunks.append(f"【{item.filename}】\n" + "\n".join(segments))
            elif item.automatic_evidence_use and (item.automatic_text or item.normalized_text):
                reliable_chunks.append(
                    f"【{item.filename}】\n{item.automatic_text or item.normalized_text}"
                )
            else:
                low_confidence.append(item.filename)
        if not reliable_chunks:
            issues = (
                "；".join(issue.message for item in materials for issue in item.issues[:1])
                or "没有提取到可用文本"
            )
            return self._failure_reply(
                f"我暂时无法从这份材料中提取可核验文本：{issues}。",
                "尚未开始证据判断，也没有生成研究结论。",
                "请更换文件、上传逐字稿，或在完成匿名化后重新上传。",
            )
        fields = state.field_values()
        fields["__material_text"] = "\n\n".join(reliable_chunks)
        fields["__material_names"] = names
        fields["__low_confidence"] = low_confidence
        self.store.save(
            ConversationState(
                state.session_id,
                state.workflow_id,
                "material_confirm",
                fields,
                state.project_values(),
            )
        )
        return self._material_confirmation_prompt(fields)

    async def _run_without_attachments(
        self,
        session_id: str,
        workflow_id: str,
        fields: dict[str, Any],
        project: dict[str, Any],
    ) -> str:
        if workflow_id in {"w3", "w4"}:
            self.store.save(
                ConversationState(session_id, workflow_id, "privacy_consent", fields, project)
            )
            return self._privacy_prompt()
        return await self._run_workflow(session_id, workflow_id, fields, None, None, project)

    async def _run_workflow(
        self,
        session_id: str,
        workflow_id: str,
        fields: dict[str, Any],
        filename: str | None,
        file_bytes: bytes | None,
        project: dict[str, Any],
    ) -> str:
        record = await self.workflow_service.store.create(workflow_id)
        workflow_fields = {key: value for key, value in fields.items() if not key.startswith("__")}
        await self.workflow_service.execute(
            record.run_id, workflow_id, workflow_fields, filename, file_bytes, None
        )
        completed = await self.workflow_service.store.get(record.run_id)
        if completed is None or completed.status != RunStatus.succeeded:
            self.store.save(ConversationState(session_id, workflow_id, "retry", fields, project))
            return self._failure_reply(
                "刚才的处理没有成功完成。",
                "你已经填写的信息和上传后确认的材料摘要仍被保留；没有把失败结果当作研究结论。",
                "回复“重新运行当前步骤”重试，回复“上一步”修改输入，或回复“主菜单”切换任务。",
            )
        title = WORKFLOW_TITLES[workflow_id]
        project = dict(project)
        statuses = dict(project.get("workflow_status", {}))
        statuses[workflow_id] = "AI_GENERATED"
        project["workflow_status"] = statuses
        results = dict(project.get("results", {}))
        results[workflow_id] = completed.final_markdown or ""
        project["results"] = results
        self.store.save(
            ConversationState(session_id, workflow_id, "result_review", fields, project)
        )
        return (
            f"## {title}（待你确认）\n\n{completed.final_markdown or ''}\n\n"
            "---\n这是一份 AI 生成的研究辅助结果，不会自动成为正式研究结论。\n\n"
            "1. 确认采用当前结果\n2. 说明我想修改的部分\n3. 重新运行当前步骤\n4. 返回主菜单"
        )

    def _go_back(self, state: ConversationState) -> str:
        if state.workflow_id is None:
            return self._menu_for(state.project_values())
        fields = state.field_values()
        project = state.project_values()
        if state.step in {"result_review", "after_confirm", "retry"}:
            self.store.save(ConversationState(state.session_id, fields={}, project=project))
            return self._menu_for(project)
        if state.step in {"privacy_consent", "privacy_help"}:
            previous = self._field_order(state.workflow_id, fields)[-1]
            self.store.save(
                ConversationState(state.session_id, state.workflow_id, previous, fields, project)
            )
            return "好的，请重新填写上一项；完成后我会再次提示上传授权。"
        order = self._field_order(state.workflow_id, fields)
        if state.step == "mode":
            return self._start_workflow(
                ConversationState(state.session_id, project=project), state.workflow_id
            )
        if state.step in {"upload", "material_confirm", "material_reclassify", "low_confidence"}:
            previous = order[-1]
        else:
            try:
                index = order.index(state.step)
                previous = order[max(0, index - 1)]
            except ValueError:
                previous = order[0]
        fields.pop(previous, None)
        self.store.save(
            ConversationState(state.session_id, state.workflow_id, previous, fields, project)
        )
        _, prompt = self._next_step(
            state.workflow_id, self._previous_step(state.workflow_id, previous, fields), fields
        )
        return f"好的，请重新填写：{prompt}"

    def _menu_for(self, project: dict[str, Any]) -> str:
        statuses = project.get("workflow_status", {})
        if not statuses:
            return MAIN_MENU
        status_names = {
            "NOT_STARTED": "未开始",
            "IN_PROGRESS": "进行中",
            "AI_GENERATED": "AI 已生成，待确认",
            "HUMAN_CONFIRMED": "已由研究者确认",
        }
        progress = "\n".join(
            f"{WORKFLOW_TITLES[workflow_id]}：{status_names.get(status, status)}"
            for workflow_id, status in statuses.items()
        )
        return f"当前项目进度：\n{progress}\n\n{MAIN_MENU}"

    def _ending_for(self, project: dict[str, Any]) -> str:
        statuses = project.get("workflow_status", {})
        if not statuses:
            return "好的，本次工作可以先到这里。尚未形成已确认的研究成果；之后可直接回来继续。"
        confirmed = [
            WORKFLOW_TITLES[key] for key, value in statuses.items() if value == "HUMAN_CONFIRMED"
        ]
        pending = [
            WORKFLOW_TITLES[key] for key, value in statuses.items() if value != "HUMAN_CONFIRMED"
        ]
        return (
            "好的，本次工作可以在这里结束。\n\n"
            f"已确认：{'、'.join(confirmed) or '暂无'}\n"
            f"仍待确认或未完成：{'、'.join(pending) or '暂无'}\n\n"
            "项目进度会保留在当前会话中。回复“主菜单”可继续，回复“导出”可获得可复制 Markdown。"
        )

    @staticmethod
    def _failure_reply(what: str, impact: str, next_step: str) -> str:
        return f"发生了什么：{what}\n\n是否影响已有数据：{impact}\n\n你现在可以做什么：{next_step}"

    def _privacy_prompt(self) -> str:
        return (
            "上传前请确认：访谈录音、逐字稿和田野笔记可能包含姓名、联系方式、单位、健康状况或家庭信息。\n\n"
            "请确认你有权使用这些材料，已取得必要同意，并已尽量删除与研究无关的身份证号、手机号和详细住址。"
            "行小道不会补造或推测被匿名化的身份信息。\n\n"
            "1. 我已确认，有权处理这些材料\n2. 先了解如何匿名化\n3. 取消上传"
        )

    @staticmethod
    def _upload_prompt() -> str:
        return (
            "请使用清小搭的“上传文件”发送原始材料。当前以平台实际能力为准："
            "可先使用文本和常见文档；音频、图片或扫描件只有在相应转写/OCR 服务配置完成后才会进入证据分析。"
            "较长材料建议分批上传。"
        )

    def _handle_privacy_consent(self, state: ConversationState, message: str) -> str:
        fields = state.field_values()
        project = state.project_values()
        if message == "1":
            self.store.save(
                ConversationState(state.session_id, state.workflow_id, "upload", fields, project)
            )
            return self._upload_prompt()
        if message == "2":
            self.store.save(
                ConversationState(
                    state.session_id, state.workflow_id, "privacy_help", fields, project
                )
            )
            return (
                "建议至少处理：姓名、手机号、身份证号、微信号、详细地址；以及容易间接识别人的具体单位、班级、罕见经历。"
                "可用“受访者A”“村干部B”等统一代号。\n\n"
                "1. 我已自行匿名化，继续上传\n2. 返回主菜单"
            )
        if message == "3":
            self.store.save(ConversationState(state.session_id, fields={}, project=project))
            return (
                "好的，尚未上传任何材料。你可以先处理匿名化，之后再从主菜单进入材料分析或结论质检。"
            )
        return "请回复 1（确认上传）、2（了解匿名化）或 3（取消上传）。"

    def _material_confirmation_prompt(self, fields: dict[str, Any]) -> str:
        names = fields.get("__material_names", [])
        low = fields.get("__low_confidence", [])
        summary = "\n".join(f"- {name}" for name in names) or "- 未识别文件名"
        warning = ""
        if low:
            warning = (
                "\n\n⚠️ 以下材料存在低置信转写/OCR或未通过证据门控："
                f"{'、'.join(low)}。它们不会静默作为关键证据使用。"
            )
        return (
            f"已收到 {len(names)} 份材料：\n{summary}\n\n"
            "我会把它们作为当前研究的原始材料处理，先做整理、初始编码、主题聚类、证据绑定以及反例识别。"
            "不会把 AI 推测写成受访者原话。"
            f"{warning}\n\n"
            "1. 分类正确，开始分析\n2. 修改材料类型或用途说明\n3. 取消本次上传"
        )

    async def _confirm_materials(self, state: ConversationState, message: str) -> str:
        fields = state.field_values()
        project = state.project_values()
        if message == "1":
            if fields.get("__low_confidence"):
                self.store.save(
                    ConversationState(
                        state.session_id, state.workflow_id, "low_confidence", fields, project
                    )
                )
                return (
                    "为避免低置信内容进入证据链，请选择：\n\n"
                    "1. 上传人工校对后的逐字稿/清晰版本\n"
                    "2. 忽略低置信片段，仅用已通过门控的内容继续\n"
                    "3. 取消本次上传"
                )
            return await self._run_cached_material(state)
        if message == "2":
            self.store.save(
                ConversationState(
                    state.session_id, state.workflow_id, "material_reclassify", fields, project
                )
            )
            return "请说明需要怎样调整，例如“政策材料只作背景，不用于经验结论”。"
        if message == "3":
            self.store.save(
                ConversationState(
                    state.session_id, state.workflow_id, "privacy_consent", fields, project
                )
            )
            return "已取消本次材料处理，尚未开始分析。你可再次确认授权后上传其他材料。"
        return "请回复 1（开始）、2（修改分类）或 3（取消）。"

    async def _handle_low_confidence(self, state: ConversationState, message: str) -> str:
        fields = state.field_values()
        project = state.project_values()
        if message == "1":
            self.store.save(
                ConversationState(state.session_id, state.workflow_id, "upload", fields, project)
            )
            return "请上传人工校对后的逐字稿或更清晰的文件；原低置信内容不会被当作关键证据。"
        if message == "2":
            return await self._run_cached_material(state)
        if message == "3":
            self.store.save(ConversationState(state.session_id, fields={}, project=project))
            return self._menu_for(project)
        return "请回复 1、2 或 3。"

    async def _run_cached_material(self, state: ConversationState) -> str:
        fields = state.field_values()
        text = str(fields.get("__material_text", ""))
        if not text:
            return self._failure_reply(
                "没有找到可用于分析的已确认材料。",
                "尚未生成研究结论。",
                "请重新上传材料，或返回主菜单。",
            )
        return await self._run_workflow(
            state.session_id,
            state.workflow_id or "w3",
            fields,
            "qingxiaodao-material.txt",
            text.encode("utf-8"),
            state.project_values(),
        )

    async def _rerun_from_state(
        self,
        session_id: str,
        workflow_id: str,
        fields: dict[str, Any],
        project: dict[str, Any],
    ) -> str:
        if workflow_id in {"w3", "w4"} and fields.get("__material_text"):
            return await self._run_workflow(
                session_id,
                workflow_id,
                fields,
                "qingxiaodao-material.txt",
                str(fields["__material_text"]).encode("utf-8"),
                project,
            )
        return await self._run_workflow(session_id, workflow_id, fields, None, None, project)

    async def _review_result(self, state: ConversationState, message: str) -> str:
        fields = state.field_values()
        project = state.project_values()
        workflow_id = state.workflow_id
        assert workflow_id is not None
        if message == "1":
            statuses = dict(project.get("workflow_status", {}))
            statuses[workflow_id] = "HUMAN_CONFIRMED"
            project["workflow_status"] = statuses
            self.store.save(
                ConversationState(state.session_id, workflow_id, "after_confirm", fields, project)
            )
            next_workflow = {"w1": "w2", "w2": "w3", "w3": "w4"}.get(workflow_id)
            if next_workflow:
                return (
                    f"已记录：当前{WORKFLOW_TITLES[workflow_id]}结果由研究者确认。"
                    "AI 生成不等于研究结论；这一步确认表示它符合你对材料和田野语境的判断。\n\n"
                    f"1. 继续{WORKFLOW_TITLES[next_workflow]}\n2. 返回主菜单\n3. 导出当前已确认成果"
                )
            return "已记录：当前证据质检结果由研究者确认。\n\n1. 返回主菜单\n2. 导出当前已确认成果"
        if message == "2":
            self.store.save(
                ConversationState(state.session_id, workflow_id, "result_modify", fields, project)
            )
            return "请直接说明想修改哪一部分。系统会把这项要求带入重新生成；你也可回复“上一步”查看当前结果。"
        if message == "3":
            return await self._rerun_from_state(state.session_id, workflow_id, fields, project)
        if message == "4":
            self.store.save(ConversationState(state.session_id, fields={}, project=project))
            return self._menu_for(project)
        return "请回复 1（确认）、2（修改）、3（重新运行）或 4（主菜单）。"

    def _after_confirmation(self, state: ConversationState, message: str) -> str:
        workflow_id = state.workflow_id
        assert workflow_id is not None
        project = state.project_values()
        next_workflow = {"w1": "w2", "w2": "w3", "w3": "w4"}.get(workflow_id)
        if next_workflow and message == "1":
            return self._start_workflow(
                ConversationState(state.session_id, fields={}, project=project), next_workflow
            )
        if (next_workflow and message == "2") or (not next_workflow and message == "1"):
            self.store.save(ConversationState(state.session_id, fields={}, project=project))
            return self._menu_for(project)
        if (next_workflow and message == "3") or (not next_workflow and message == "2"):
            return self._export_reply(project)
        return "请按当前菜单回复数字；也可随时回复“主菜单”。"

    @staticmethod
    def _export_reply(project: dict[str, Any]) -> str:
        results = project.get("results", {})
        confirmed = project.get("workflow_status", {})
        parts = ["# 行小道研究工作包\n\n以下仅包含研究者已确认的成果。"]
        for workflow_id in ("w1", "w2", "w3", "w4"):
            if confirmed.get(workflow_id) == "HUMAN_CONFIRMED" and results.get(workflow_id):
                parts.append(f"## {WORKFLOW_TITLES[workflow_id]}\n\n{results[workflow_id]}")
        if len(parts) == 1:
            return "目前还没有研究者已确认的成果，因此不能导出为正式研究工作包。你可先确认某一步的结果。"
        return (
            "当前清小搭接入尚未配置真实文件导出，所以我先生成可复制的 Markdown：\n\n"
            + "\n\n".join(parts)
        )

    @staticmethod
    def _revision_field(workflow_id: str) -> str:
        return {
            "w1": "background",
            "w2": "sensitive_topics",
            "w3": "source_context",
            "w4": "source_context",
        }[workflow_id]

    @staticmethod
    def _safety_reply(message: str) -> str | None:
        text = message.lower()
        if any(word in text for word in ("编造访谈", "虚构访谈", "造访谈", "伪造数据")):
            return (
                "我不能把虚构访谈、数据或引文当作真实研究材料。"
                "如果你的目的是测试系统，我可以协助生成明确标注为“模拟测试材料”的样本，但它不能用于真实调研证据。\n\n"
                "回复“主菜单”选择其他任务。"
            )
        if ("失业率" in text or "比例" in text or "全国" in text) and any(
            word in text for word in ("访谈", "质性", "材料")
        ):
            return (
                "这个任务超出了当前材料和方法能够可靠支持的范围。少量质性访谈适合帮助理解类型、过程、意义和机制，"
                "不能据此推断全国或总体比例。\n\n"
                "我可以帮你把问题改成适合质性研究的问题，或设计量化问卷研究框架。"
            )
        return None

    @staticmethod
    def _candidate_workflows(message: str) -> list[str]:
        text = message.lower()
        candidates: list[str] = []
        if any(
            word in text for word in ("编码", "主题", "访谈记录", "田野", "材料一大堆", "材料分析")
        ):
            candidates.append("w3")
        if any(word in text for word in ("质检", "核验", "结论", "证据", "反例", "站得住")):
            candidates.append("w4")
        if any(word in text for word in ("访谈", "提纲", "问题清单")):
            candidates.append("w2")
        if any(word in text for word in ("选题", "研究问题", "研究设计", "社会实践", "想法")):
            candidates.append("w1")
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _route_reason(workflow_id: str) -> str:
        return {
            "w1": "你的问题还需要收敛为研究对象清楚、边界可执行的研究设计。",
            "w2": "你已经在准备接触受访者，下一步是明确对象与问题设计。",
            "w3": "你已经拥有原始材料，适合先整理、编码并把主题对应回证据。",
            "w4": "你已经形成了判断，需要检查证据、反例、样本边界和推断风险。",
        }[workflow_id]

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
