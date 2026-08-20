"""清小搭对话编排层。

这个模块只负责把平台中的自然语言会话变成可执行的研究步骤；研究
方法生成、证据核验仍由 ``WorkflowService`` 完成。会话状态只保存用户
确认输入的简短字段，不保存附件下载地址或模型提示词。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, cast
from uuid import uuid4

from app.dialogue import OpenDialogueResponder
from app.models import IntentRouteResult, RunStatus
from app.multimodal.contracts import FileContentPart, ImageUrlContentPart, InputAudioContentPart
from app.multimodal.models import Material
from app.routing import IntentRouter

MAIN_MENU = """你好，我是行小道，你的社会实践与社会科学调研智能助手。

我可以陪你完成研究设计、访谈设计、材料分析、证据质检与补访建议。直接用自己的话说出你现在遇到的问题就可以；如果你愿意，也可选择：

1. 研究设计：把一个想法变成研究方案
2. 访谈设计：设计或检查访谈
3. 材料分析：整理已有访谈或田野材料
4. 结论质检：检查研究结论是否站得住

行小道不会虚构访谈、编造数据或原始引文，也不会把有限质性样本包装成总体结论。AI 的建议需要由你结合原始材料和导师意见确认。

你随时可以说“回到项目主页”“上一步”“结束”“新建项目：项目名称”或“项目列表”。"""

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
        self, attachments: list[InputAudioContentPart | ImageUrlContentPart | FileContentPart]
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
        """Return the active project while keeping its session workspace attached.

        The attachment is internal-only (``__workspace``).  It lets the older
        workflow code continue to pass a plain project dict around, while the
        store persists every project's own fields and interaction state.
        """

        workspace = ConversationStore.normalise_workspace(self.project or {})
        active_id = workspace["active_project_id"]
        project = cast(dict[str, Any], json.loads(json.dumps(workspace["projects"][active_id])))
        project["__workspace"] = workspace
        project["__active_project_id"] = active_id
        return project


class ConversationStore:
    """SQLite-backed, per-session project workspace.

    ``conversation_state`` is deliberately kept for backward compatibility.
    Its JSON column now contains a lightweight workspace, not a single global
    project: several named projects can coexist in one Qingxiaoda session.
    """

    ARCHIVE_RETENTION_DAYS = 30

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

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @classmethod
    def _new_project(cls, name: str = "当前研究项目") -> dict[str, Any]:
        now = cls._now()
        return {
            "project_id": uuid4().hex,
            "project_name": name,
            "current_stage": "项目主页",
            "current_workflow": "",
            "research_topic": "",
            "research_question": "",
            "target_group": "",
            "research_location": "",
            "research_method": "",
            "uploaded_materials": [],
            "confirmed_findings": [],
            "pending_questions": [],
            "workflow_status": {
                "w1": "NOT_STARTED",
                "w2": "NOT_STARTED",
                "w3": "NOT_STARTED",
                "w4": "NOT_STARTED",
            },
            "results": {},
            "last_user_intent": "",
            "last_system_action": "",
            "active_menu": {"menu_id": "main_menu", "status": "ACTIVE", "options": {}},
            "interaction": {"workflow_id": None, "step": "menu", "fields": {}},
            "archived_at": None,
            "created_at": now,
            "updated_at": now,
        }

    @classmethod
    def normalise_workspace(cls, saved: dict[str, Any]) -> dict[str, Any]:
        """Migrate legacy single-project JSON in place, without losing work."""

        value = cast(dict[str, Any], json.loads(json.dumps(saved or {})))
        if "projects" in value and "active_project_id" in value:
            workspace = value
        else:
            legacy = value
            project = cls._new_project(legacy.get("project_name") or "当前研究项目")
            for key, item in legacy.items():
                if key not in {"__workspace", "__active_project_id"}:
                    project[key] = item
            project.setdefault("interaction", {})
            project["interaction"].setdefault("workflow_id", None)
            project["interaction"].setdefault("step", "menu")
            project["interaction"].setdefault("fields", {})
            workspace = {
                "schema_version": 2,
                "active_project_id": project["project_id"],
                "projects": {project["project_id"]: project},
                "pending_action": None,
            }
        workspace.setdefault("schema_version", 2)
        workspace.setdefault("pending_action", None)
        projects = workspace.setdefault("projects", {})
        if not projects:
            project = cls._new_project()
            projects[project["project_id"]] = project
            workspace["active_project_id"] = project["project_id"]
        if workspace.get("active_project_id") not in projects:
            workspace["active_project_id"] = next(iter(projects))
        for project_id, project in list(projects.items()):
            if not isinstance(project, dict):
                projects[project_id] = cls._new_project("当前研究项目")
                project = projects[project_id]
            project.setdefault("project_id", project_id)
            project.setdefault("project_name", "当前研究项目")
            for key in (
                "current_stage", "current_workflow", "research_topic", "research_question",
                "target_group", "research_location", "research_method", "last_user_intent",
                "last_system_action",
            ):
                project.setdefault(key, "")
            for key in ("uploaded_materials", "confirmed_findings", "pending_questions"):
                project.setdefault(key, [])
            project.setdefault("results", {})
            statuses = project.setdefault("workflow_status", {})
            for workflow_id in WORKFLOW_TITLES:
                statuses.setdefault(workflow_id, "NOT_STARTED")
            project.setdefault("active_menu", {"menu_id": "main_menu", "status": "ACTIVE", "options": {}})
            interaction = project.setdefault("interaction", {})
            interaction.setdefault("workflow_id", None)
            interaction.setdefault("step", "menu")
            interaction.setdefault("fields", {})
            project.setdefault("archived_at", None)
            project.setdefault("created_at", cls._now())
            project.setdefault("updated_at", cls._now())
        cls._cleanup_archived(workspace)
        return cast(dict[str, Any], workspace)

    @classmethod
    def _cleanup_archived(cls, workspace: dict[str, Any]) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=cls.ARCHIVE_RETENTION_DAYS)
        active_id = workspace.get("active_project_id")
        for project_id, project in list(workspace.get("projects", {}).items()):
            archived_at = project.get("archived_at")
            if not archived_at or project_id == active_id:
                continue
            try:
                archived_time = datetime.fromisoformat(str(archived_at))
                if archived_time.tzinfo is None:
                    archived_time = archived_time.replace(tzinfo=UTC)
            except ValueError:
                continue
            if archived_time < cutoff:
                del workspace["projects"][project_id]

    @classmethod
    def _workspace_for_save(cls, state: ConversationState) -> dict[str, Any]:
        raw_project = dict(state.project or {})
        embedded = raw_project.pop("__workspace", None)
        active_id = raw_project.pop("__active_project_id", None)
        workspace = cls.normalise_workspace(embedded if isinstance(embedded, dict) else raw_project)
        active_id = active_id or workspace["active_project_id"]
        if active_id not in workspace["projects"]:
            active_id = workspace["active_project_id"]
        project = workspace["projects"][active_id]
        # A state save is the sole write-back point for workflow navigation.
        # This keeps every project's fields isolated when the user switches.
        for key, value in raw_project.items():
            if not key.startswith("__"):
                project[key] = value
        project["interaction"] = {
            "workflow_id": state.workflow_id,
            "step": state.step,
            "fields": state.field_values(),
        }
        cls._sync_project_context(project, state.workflow_id, state.field_values())
        project["current_workflow"] = state.workflow_id or ""
        project["current_stage"] = WORKFLOW_TITLES.get(state.workflow_id or "", "项目主页")
        project["active_menu"] = cls._menu_for_state(state.workflow_id, state.step, project)
        project["updated_at"] = cls._now()
        workspace["active_project_id"] = active_id
        cls._cleanup_archived(workspace)
        return workspace

    @classmethod
    def _sync_project_context(
        cls, project: dict[str, Any], workflow_id: str | None, fields: dict[str, Any]
    ) -> None:
        """Persist only fields that the user typed or explicitly retained.

        This intentionally contains no LLM extraction: no missing fact is
        invented, and the conflict gate in ``QingxiaodaConversation`` runs
        before a different value can be written.
        """

        field_map = {
            "theme": "research_topic",
            "research_question": "research_question",
            "review_topic": "research_question",
            "participant_profile": "target_group",
            "participants": "target_group",
        }
        for source, destination in field_map.items():
            value = str(fields.get(source, "")).strip()
            if value:
                project[destination] = value
        if workflow_id == "w1" and fields.get("purpose"):
            project["research_method"] = "社会实践研究设计（待研究者确认）"
        if workflow_id == "w2" and fields.get("mode"):
            project["research_method"] = "半结构式访谈（待研究者确认）"
        if workflow_id == "w3" and fields.get("__material_names"):
            known = {str(item.get("name")) for item in project.get("uploaded_materials", []) if isinstance(item, dict)}
            for name in fields["__material_names"]:
                if name not in known:
                    project.setdefault("uploaded_materials", []).append(
                        {"name": name, "added_at": cls._now(), "status": "已读取，待研究者确认用途"}
                    )

    @staticmethod
    def _menu_for_state(
        workflow_id: str | None, step: str, project: dict[str, Any]
    ) -> dict[str, Any]:
        options: dict[str, str] = {}
        menu_id = "free_text"
        if workflow_id is None or step == "menu":
            menu_id, options = "main_menu", {"1": "START_W1", "2": "START_W2", "3": "START_W3", "4": "START_W4"}
        elif step == "mode":
            menu_id, options = "interview_mode", {"1": "GENERATE_INTERVIEW", "2": "REVIEW_INTERVIEW"}
        elif step == "source_type":
            menu_id, options = "material_type", {"1": "ONE_INTERVIEW", "2": "MANY_INTERVIEWS", "3": "FIELD_NOTES", "4": "MIXED_MATERIALS"}
        elif step == "privacy_consent":
            menu_id, options = "privacy_consent", {"1": "CONSENT_UPLOAD", "2": "ANONYMISATION_HELP", "3": "CANCEL_UPLOAD"}
        elif step == "privacy_help":
            menu_id, options = "privacy_help", {"1": "CONTINUE_UPLOAD", "2": "RETURN_PROJECT_HOME"}
        elif step == "material_confirm":
            menu_id, options = "material_confirm", {"1": "ANALYSE_MATERIALS", "2": "RECLASSIFY_MATERIALS", "3": "CANCEL_MATERIALS"}
        elif step == "low_confidence":
            menu_id, options = "low_confidence_material", {"1": "UPLOAD_CORRECTED", "2": "USE_HIGH_CONFIDENCE_ONLY", "3": "CANCEL_MATERIALS"}
        elif step == "result_review":
            menu_id, options = "result_review", {"1": "CONFIRM_RESULT", "2": "MODIFY_RESULT", "3": "RERUN_WORKFLOW", "4": "RETURN_PROJECT_HOME"}
        elif step == "after_confirm":
            menu_id = "after_confirmation"
            options = {"1": "CONTINUE_NEXT" if workflow_id != "w4" else "RETURN_PROJECT_HOME", "2": "RETURN_PROJECT_HOME" if workflow_id != "w4" else "EXPORT_CONFIRMED"}
            if workflow_id != "w4":
                options["3"] = "EXPORT_CONFIRMED"
        elif step == "conflict_confirm":
            menu_id, options = "context_conflict", {"1": "REPLACE_CONTEXT", "2": "KEEP_CONTEXT"}
        return {"menu_id": menu_id, "status": "ACTIVE" if options else "NONE", "options": options, "scope": workflow_id or "project"}

    def get(self, session_id: str) -> ConversationState:
        with self._lock:
            row = self._connection.execute(
                "SELECT workflow_id, step, fields_json, project_json FROM conversation_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return ConversationState(session_id=session_id, project={})
        workspace = self.normalise_workspace(json.loads(row[3]))
        project = workspace["projects"][workspace["active_project_id"]]
        interaction = project.get("interaction", {})
        return ConversationState(
            session_id=session_id,
            workflow_id=interaction.get("workflow_id", row[0]),
            step=interaction.get("step", row[1]),
            fields=interaction.get("fields", json.loads(row[2])),
            project=workspace,
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
                    json.dumps(self._workspace_for_save(state), ensure_ascii=False),
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
        intent_router: IntentRouter | None = None,
        dialogue_responder: OpenDialogueResponder | None = None,
    ) -> None:
        self.store = ConversationStore(database_path)
        self.workflow_service = workflow_service
        self.material_ingestor = material_ingestor
        self.intent_router = intent_router
        self.dialogue_responder = dialogue_responder

    async def reply(
        self,
        *,
        session_id: str | None,
        text: str,
        attachments: list[InputAudioContentPart | ImageUrlContentPart | FileContentPart],
    ) -> str:
        # 清小搭正式聊天会提供 sessionId。无 sessionId 的普通协议调用仍可
        # 获得菜单，但不会把状态误共享给其他调用者。
        safe_session_id = (session_id or f"temporary-{uuid4().hex}")[:200]
        state = self.store.get(safe_session_id)
        message = text.strip()

        project_command = self._handle_project_command(state, message)
        if project_command is not None:
            return project_command

        if self._is_project_home(message):
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

        # Natural-language recommendations are intentionally optional.  If the
        # user accepts the latest one, make its “1” unambiguous instead of
        # treating it as the global W1 menu choice.
        pending_workflow = str(state.project_values().get("pending_workflow", ""))
        if (
            state.workflow_id is None
            and pending_workflow in WORKFLOW_TITLES
            and self._accept_pending_workflow(message)
        ):
            project = state.project_values()
            project.pop("pending_workflow", None)
            return self._start_workflow(
                ConversationState(safe_session_id, fields={}, project=project), pending_workflow
            )

        # An explicit natural-language request is always stronger than an old
        # menu.  The transition writes a new interaction state, invalidating
        # the previous numeric menu before the next user turn.
        explicit_workflow = self._explicit_workflow_intent(message)
        if explicit_workflow is not None:
            project = state.project_values()
            project["last_user_intent"] = f"切换至{WORKFLOW_TITLES[explicit_workflow]}"
            return self._start_workflow(
                ConversationState(safe_session_id, fields={}, project=project), explicit_workflow
            )
        if self._is_export_intent(message):
            self.store.save(ConversationState(safe_session_id, fields={}, project=state.project_values()))
            return self._export_reply(state.project_values())

        if state.workflow_id is None:
            return await self._choose_workflow(state, message, attachments)
        if not attachments and self._looks_like_open_question(message):
            return await self._answer_in_workflow_context(state, message)
        return await self._continue_workflow(state, message, attachments)

    @staticmethod
    def _is_project_home(message: str) -> bool:
        return message.strip() in {
            "主菜单", "菜单", "0", "重新开始", "重新选择任务", "项目主页", "回到项目主页",
            "返回项目主页", "回主页",
        }

    @staticmethod
    def _is_export_intent(message: str) -> bool:
        return any(token in message for token in ("导出", "下载当前成果", "导出成果"))

    @staticmethod
    def _explicit_workflow_intent(message: str) -> str | None:
        """Recognise only clear task switches; never infer facts from it."""

        text = message.lower()
        if not any(action in text for action in ("进入", "开始", "切换", "使用", "打开")):
            return None
        if any(token in text for token in ("改访谈", "修改访谈", "访谈提纲", "访谈问题", "设计访谈")):
            return "w2"
        if any(token in text for token in ("分析材料", "分析访谈", "主题分析", "编码", "提炼主题", "田野笔记")):
            return "w3"
        if any(token in text for token in ("核验证据", "检查结论", "结论质检", "反例", "证据是否")):
            return "w4"
        if any(token in text for token in ("研究设计", "收窄选题", "聚焦选题", "变成研究问题")):
            return "w1"
        return None

    def _handle_project_command(self, state: ConversationState, message: str) -> str | None:
        """Handle lightweight named projects within one Qingxiaoda session."""

        project = state.project_values()
        workspace = project["__workspace"]
        pending = workspace.get("pending_action")
        if pending and pending.get("type") == "archive":
            if message in {"1", "确认", "确认归档", "好的", "是"}:
                target = workspace["projects"].get(pending["project_id"])
                if target is None:
                    workspace["pending_action"] = None
                    self.store.save(ConversationState(state.session_id, project=project))
                    return "要归档的项目已经不存在了。"
                target["archived_at"] = ConversationStore._now()
                workspace["pending_action"] = None
                available = [
                    candidate for candidate in workspace["projects"].values()
                    if candidate["project_id"] != target["project_id"] and not candidate.get("archived_at")
                ]
                if available:
                    workspace["active_project_id"] = available[0]["project_id"]
                else:
                    new_project = ConversationStore._new_project("未命名研究项目")
                    workspace["projects"][new_project["project_id"]] = new_project
                    workspace["active_project_id"] = new_project["project_id"]
                active_view = self._project_view(workspace, workspace["active_project_id"])
                self.store.save(ConversationState(state.session_id, project=active_view))
                return "已归档该项目。归档内容会保留 30 天；你可以说“查看归档项目”或“恢复项目：项目名称”。\n\n" + self._menu_for(active_view)
            if message in {"2", "取消", "不用了", "否"}:
                workspace["pending_action"] = None
                self.store.save(ConversationState(state.session_id, project=project))
                return "好的，项目仍保留在当前列表中。"
            return "是否归档这个项目？回复“确认归档”或“取消”。"

        if message in {"项目列表", "查看项目", "我的项目"}:
            active_id = workspace["active_project_id"]
            active_lines: list[str] = []
            archived_lines: list[str] = []
            for item in workspace["projects"].values():
                marker = "（当前）" if item["project_id"] == active_id else ""
                line = f"- {item['project_name']}{marker}"
                (archived_lines if item.get("archived_at") else active_lines).append(line)
            response = "当前项目：\n" + ("\n".join(active_lines) or "- 暂无")
            if archived_lines:
                response += "\n\n已归档：\n" + "\n".join(archived_lines)
            return response + "\n\n可直接说“切换项目：项目名称”或“新建项目：项目名称”。"
        if message in {"查看归档项目", "归档项目列表"}:
            archived_projects = [item for item in workspace["projects"].values() if item.get("archived_at")]
            if not archived_projects:
                return "目前没有已归档项目。"
            return "已归档项目（归档后最多保留 30 天）：\n" + "\n".join(
                f"- {item['project_name']}" for item in archived_projects
            ) + "\n\n如需继续，可说“恢复项目：项目名称”。"

        command, separator, name = message.partition("：")
        if not separator:
            command, separator, name = message.partition(":")
        if not separator:
            return None
        name = name.strip()
        if command.strip() not in {"新建项目", "切换项目", "归档项目", "恢复项目"}:
            return None
        if not name:
            return "请在冒号后补充项目名称，例如“新建项目：返乡青年创业调研”。"
        matches = [item for item in workspace["projects"].values() if item["project_name"] == name]
        if command.strip() == "新建项目":
            if matches:
                existing = matches[0]
                action = "恢复" if existing.get("archived_at") else "切换"
                return f"已存在名为“{name}”的项目。请使用“{action}项目：{name}”，或换一个名称。"
            new_project = ConversationStore._new_project(name)
            workspace["projects"][new_project["project_id"]] = new_project
            workspace["active_project_id"] = new_project["project_id"]
            active = self._project_view(workspace, new_project["project_id"])
            self.store.save(ConversationState(state.session_id, project=active))
            return f"已新建项目“{name}”。\n\n" + self._menu_for(new_project)
        if not matches:
            return f"没有找到“{name}”。可先说“项目列表”查看名称。"
        target = matches[0]
        if command.strip() == "切换项目":
            if target.get("archived_at"):
                return f"“{name}”已归档。请先说“恢复项目：{name}”。"
            workspace["active_project_id"] = target["project_id"]
            active = self._project_view(workspace, target["project_id"])
            self.store.save(ConversationState(state.session_id, project=active))
            return f"已切换到“{name}”。\n\n" + self._menu_for(target)
        if command.strip() == "归档项目":
            if target.get("archived_at"):
                return f"“{name}”已经归档。"
            workspace["pending_action"] = {"type": "archive", "project_id": target["project_id"]}
            self.store.save(ConversationState(state.session_id, project=project))
            return f"归档后“{name}”会从日常项目列表隐藏，并在 30 天后清理。确认归档吗？\n\n1. 确认归档\n2. 取消"
        if not target.get("archived_at"):
            return f"“{name}”当前没有归档，可以直接切换。"
        target["archived_at"] = None
        workspace["active_project_id"] = target["project_id"]
        active = self._project_view(workspace, target["project_id"])
        self.store.save(ConversationState(state.session_id, project=active))
        return f"已恢复并切换到“{name}”。\n\n" + self._menu_for(target)

    @staticmethod
    def _project_view(workspace: dict[str, Any], project_id: str) -> dict[str, Any]:
        view = cast(dict[str, Any], json.loads(json.dumps(workspace["projects"][project_id])))
        view["__workspace"] = workspace
        view["__active_project_id"] = project_id
        return view

    @staticmethod
    def _accept_pending_workflow(message: str) -> bool:
        normalized = message.strip().lower()
        return normalized in {
            "1", "开始", "进入", "继续", "好", "好的", "可以", "开始吧",
            "进入工作流", "开始整理", "帮我生成",
        }

    @staticmethod
    def _looks_like_open_question(message: str) -> bool:
        text = message.strip()
        if len(text) > 120:
            return False
        # During a structured form, a sentence containing “如何” may itself
        # be the research question the user is supplying.  Only intercept an
        # unmistakable help/question turn, so normal field collection wins.
        return text.startswith((
            "为什么", "怎么", "能不能", "可以", "是否可以", "你能", "什么意思", "请问",
        ))

    async def _open_dialogue_reply(
        self,
        state: ConversationState,
        message: str,
        route: IntentRouteResult | None,
    ) -> str:
        project = state.project_values()
        recommended = ""
        if (
            route is not None
            and route.recommended_workflow in WORKFLOW_TITLES
            and route.confidence in {"high", "medium"}
        ):
            recommended = str(route.recommended_workflow)
            project["pending_workflow"] = recommended
        else:
            project.pop("pending_workflow", None)
        self.store.save(ConversationState(state.session_id, fields={}, project=project))

        if self.dialogue_responder is not None:
            answer = await self.dialogue_responder.respond(message=message, route=route)
        else:
            answer = "我先根据你的情况给出建议；如需进一步整理，也可以继续补充。"
        if not recommended:
            return answer + "\n\n如果你愿意，可以继续补充你的实践场景、已有材料或最想解决的问题。"
        title = WORKFLOW_TITLES[recommended]
        return (
            answer
            + f"\n\n如果你希望把这件事整理成一套可执行的{title}，回复 **1** 即可；"
            "也可以继续直接和我讨论。"
        )

    async def _answer_in_workflow_context(self, state: ConversationState, message: str) -> str:
        if self.dialogue_responder is None:
            return "可以继续说明你的困惑；当前表单不会因为这次提问而提交或改写。"
        answer = await self.dialogue_responder.respond(
            message=message,
            route=None,
            active_workflow=state.workflow_id,
        )
        return answer + "\n\n当前流程仍保留在原步骤；要继续填写，直接发送该步骤所需信息即可。"

    async def _choose_workflow(
        self,
        state: ConversationState,
        message: str,
        attachments: list[InputAudioContentPart | ImageUrlContentPart | FileContentPart],
    ) -> str:
        selection = self._selection(message)
        if selection is None and attachments:
            selection = "w3"
        # A free-text description should lead to a useful answer first.  A
        # workflow remains an opt-in next step; it is not a forced form.
        if selection is None and not attachments and self.intent_router is not None:
            try:
                routed = await self.intent_router.route(message)
                return await self._open_dialogue_reply(state, message, routed)
            except Exception:
                # The model must never make the conversation unavailable.
                pass
        if selection is None:
            candidates = self._candidate_workflows(message)
            if False and len(candidates) == 2:  # legacy deterministic fallback; keep dialogue first
                first, second = candidates
                return (
                    "我还需要确认一件事：你现在是准备收集材料，还是已经有材料需要处理？\n\n"
                    f"- 如果在准备访谈或观察，我可以先帮你做{WORKFLOW_TITLES[first]}；\n"
                    f"- 如果已有逐字稿、录音或田野笔记，我可以先做{WORKFLOW_TITLES[second]}。\n\n"
                    "直接说说你手头已有的材料即可。"
                )
            return await self._open_dialogue_reply(state, message, None)
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
        project["current_workflow"] = workflow_id
        project["current_stage"] = WORKFLOW_TITLES[workflow_id]
        project["last_system_action"] = f"开始{WORKFLOW_TITLES[workflow_id]}"
        if workflow_id == "w1":
            if project.get("research_topic"):
                fields["theme"] = project["research_topic"]
                next_state = ConversationState(state.session_id, "w1", "purpose", fields, project)
                prompt = f"好，我们继续完善研究设计。当前主题是“{project['research_topic']}”。\n\n你希望通过这项研究理解、解释或改进什么？"
            else:
                next_state = ConversationState(state.session_id, "w1", "theme", fields, project)
                prompt = "好，我们先把想法收成可执行的研究设计。\n\n第一步：请用一句话描述你想研究的社会实践主题。"
        elif workflow_id == "w2":
            next_state = ConversationState(state.session_id, "w2", "mode", fields, project)
            prompt = "好，我们来处理访谈。你是想从零起草提纲，还是已经有一组问题希望我审查？\n\n1. 起草访谈提纲\n2. 审查已有问题"
        elif workflow_id == "w3":
            if project.get("research_question"):
                fields["research_question"] = project["research_question"]
                step = "source_id"
                prompt = f"好，我们直接看已有材料。当前研究问题是“{project['research_question']}”。\n\n请给这批材料取一个便于引用的名称或编号，例如“访谈材料包 A”。"
            else:
                step = "research_question"
                prompt = "好，我们开始材料分析。\n\n第一步：这批材料要回答的研究问题是什么？"
            next_state = ConversationState(
                state.session_id, "w3", step, fields, project
            )
        else:
            if project.get("research_question"):
                fields["research_question"] = project["research_question"]
                step = "candidate_claim"
                prompt = f"好，我们检查这项研究的结论边界。当前研究问题是“{project['research_question']}”。\n\n请写出需要核验的一条结论或判断。"
            else:
                step = "research_question"
                prompt = "好，我们先明确要检查的研究问题。\n\n请写出这项研究希望回答的研究问题。"
            next_state = ConversationState(
                state.session_id, "w4", step, fields, project
            )
        self.store.save(next_state)
        return prompt

    def _resolve_menu_input(self, state: ConversationState, message: str) -> str:
        """Map a numeric or natural-language answer only inside its active menu.

        All existing workflow branches can still consume their small canonical
        values (``"1"``, ``"2"`` …), but they never receive a value from an
        earlier screen: the current ``step`` owns the menu mapping.
        """

        project = state.project_values()
        menu = project.get("active_menu", {})
        options = menu.get("options", {}) if menu.get("status") == "ACTIVE" else {}
        if message.strip().isdigit():
            return message if message in options else message
        lowered = message.lower()
        step = state.step
        natural: dict[str, tuple[tuple[str, ...], str]] = {
            "mode": (("从零", "起草", "生成", "新建", "设计"), "1"),
            "source_type": (("单份", "一个受访者"), "1"),
            "privacy_consent": (("确认", "有权", "同意", "可以上传"), "1"),
            "privacy_help": (("已匿名", "继续", "上传"), "1"),
            "material_confirm": (("分类正确", "开始分析", "开始", "确认分析"), "1"),
            "low_confidence": (("上传校对", "更清晰", "重新上传"), "1"),
            "result_review": (("确认采用", "确认结果", "采用", "确认"), "1"),
            "after_confirm": (("继续", "下一步"), "1"),
            "conflict_confirm": (("替换", "更新", "使用新的", "确认修改"), "1"),
        }
        # Multi-choice menus whose text could otherwise overlap are handled
        # before their generic positive action.
        if step == "mode":
            if any(token in lowered for token in ("审查", "检查已有", "已有问题", "修改已有")):
                return "2"
        elif step == "source_type":
            if any(token in lowered for token in ("多份", "多位", "多个人")):
                return "2"
            if any(token in lowered for token in ("田野", "观察")):
                return "3"
            if "混合" in lowered:
                return "4"
        elif step == "privacy_consent":
            if "匿名" in lowered and not any(token in lowered for token in ("已匿名", "匿名化完成")):
                return "2"
            if any(token in lowered for token in ("取消", "不上传", "暂不")):
                return "3"
        elif step == "privacy_help":
            if any(token in lowered for token in ("主菜单", "项目主页", "返回")):
                return "2"
        elif step == "material_confirm":
            if any(token in lowered for token in ("修改", "分类", "用途说明")):
                return "2"
            if any(token in lowered for token in ("取消", "不分析", "暂不")):
                return "3"
        elif step == "low_confidence":
            if any(token in lowered for token in ("只用", "忽略低置信", "高置信")):
                return "2"
            if any(token in lowered for token in ("取消", "不分析", "暂不")):
                return "3"
        elif step == "result_review":
            if any(token in lowered for token in ("修改", "调整", "不符合", "不对")):
                return "2"
            if any(token in lowered for token in ("重新运行", "重做", "再跑")):
                return "3"
            if any(token in lowered for token in ("主菜单", "项目主页", "返回")):
                return "4"
        elif step == "after_confirm":
            if any(token in lowered for token in ("导出", "下载")):
                return "3" if state.workflow_id != "w4" else "2"
            if any(token in lowered for token in ("主页", "主菜单", "返回")):
                return "2" if state.workflow_id != "w4" else "1"
        elif step == "conflict_confirm":
            if any(token in lowered for token in ("保留", "不改", "原来的", "取消")):
                return "2"
        tokens_and_choice = natural.get(step)
        if tokens_and_choice and any(token in lowered for token in tokens_and_choice[0]):
            return tokens_and_choice[1]
        return message

    @staticmethod
    def _context_key_for_field(field: str) -> str | None:
        return {
            "theme": "research_topic",
            "research_question": "research_question",
            "review_topic": "research_question",
            "participant_profile": "target_group",
            "participants": "target_group",
        }.get(field)

    def _context_conflict(self, project: dict[str, Any], field: str, value: str) -> str | None:
        context_key = self._context_key_for_field(field)
        existing = str(project.get(context_key, "")).strip() if context_key else ""
        if not existing or existing == value.strip():
            return None
        assert context_key is not None
        label = {
            "research_topic": "研究主题",
            "research_question": "研究问题",
            "target_group": "研究对象",
        }[context_key]
        return (
            f"你刚补充的{label}与当前项目已记录的信息不一致。\n\n"
            f"当前：{existing}\n新的：{value.strip()}\n\n"
            "要用新的内容替换当前项目记录吗？\n1. 替换为新的内容\n2. 保留当前内容"
        )

    def _handle_context_conflict(self, state: ConversationState, message: str) -> str:
        fields = state.field_values()
        project = state.project_values()
        field = str(fields.pop("__pending_context_field", ""))
        value = str(fields.pop("__pending_context_value", "")).strip()
        if not field or not value:
            self.store.save(ConversationState(state.session_id, state.workflow_id, "menu", fields, project))
            return self._menu_for(project)
        if message == "1":
            fields[field] = value
            project["last_system_action"] = "已按研究者确认更新项目上下文"
            return self._advance_after_context_resolution(state, fields, project, field)
        if message == "2":
            context_key = self._context_key_for_field(field)
            if context_key:
                fields[field] = str(project.get(context_key, ""))
            return self._advance_after_context_resolution(state, fields, project, field)
        return "请回复“替换为新的内容”或“保留当前内容”。"

    def _advance_after_context_resolution(
        self,
        state: ConversationState,
        fields: dict[str, Any],
        project: dict[str, Any],
        field: str,
    ) -> str:
        workflow_id = state.workflow_id
        assert workflow_id is not None
        next_step, next_prompt = self._next_step(workflow_id, field, fields)
        if next_step is not None:
            self.store.save(ConversationState(state.session_id, workflow_id, next_step, fields, project))
            return next_prompt
        return "已更新当前项目记录。请继续说明你希望如何推进；也可以回到项目主页。"

    async def _continue_workflow(
        self,
        state: ConversationState,
        message: str,
        attachments: list[InputAudioContentPart | ImageUrlContentPart | FileContentPart],
    ) -> str:
        workflow_id = state.workflow_id
        assert workflow_id is not None
        fields = state.field_values()
        project = state.project_values()

        message = self._resolve_menu_input(state, message)

        if state.step == "conflict_confirm":
            return self._handle_context_conflict(state, message)

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
            if message == "1" and project.get("research_question"):
                fields["research_question"] = project["research_question"]
                step = "participant_profile"
            else:
                step = "research_question" if message == "1" else "review_topic"
            self.store.save(ConversationState(state.session_id, workflow_id, step, fields, project))
            return (
                (f"我会沿用当前研究问题：“{project['research_question']}”。\n\n计划访谈谁？请描述对象范围或筛选条件。"
                 if message == "1" and project.get("research_question") else "请写出你想回答的研究问题。")
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
        conflict = self._context_conflict(project, state.step, message)
        if conflict is not None:
            fields["__pending_context_field"] = state.step
            fields["__pending_context_value"] = message
            self.store.save(
                ConversationState(state.session_id, workflow_id, "conflict_confirm", fields, project)
            )
            return conflict
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
        attachments: list[InputAudioContentPart | ImageUrlContentPart | FileContentPart],
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
            "请先结合你的田野判断核对它。你可以直接说“确认采用”“我想修改……”或“重新运行”。\n\n"
            "1. 确认采用当前结果\n2. 说明我想修改的部分\n3. 重新运行当前步骤\n4. 返回项目主页"
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
        status_names = {
            "NOT_STARTED": "未开始",
            "IN_PROGRESS": "进行中",
            "AI_GENERATED": "AI 已生成，待确认",
            "HUMAN_CONFIRMED": "已由研究者确认",
        }
        progress = "\n".join(
            f"- {WORKFLOW_TITLES[workflow_id]}：{status_names.get(statuses.get(workflow_id), statuses.get(workflow_id))}"
            for workflow_id in WORKFLOW_TITLES
        )
        known = []
        if project.get("research_topic"):
            known.append(f"研究主题：{project['research_topic']}")
        if project.get("research_question"):
            known.append(f"研究问题：{project['research_question']}")
        if project.get("target_group"):
            known.append(f"研究对象：{project['target_group']}")
        summary = "\n".join(f"- {item}" for item in known) or "- 还没有记录核心研究信息"
        return (
            f"当前项目：{project.get('project_name') or '当前研究项目'}\n\n"
            f"已知信息：\n{summary}\n\n"
            f"进度：\n{progress}\n\n{MAIN_MENU}"
        )

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
            confirmed = list(project.get("confirmed_findings", []))
            result = str(project.get("results", {}).get(workflow_id, "")).strip()
            if result and not any(item.get("workflow_id") == workflow_id for item in confirmed if isinstance(item, dict)):
                confirmed.append(
                    {
                        "workflow_id": workflow_id,
                        "title": WORKFLOW_TITLES[workflow_id],
                        "confirmed_at": ConversationStore._now(),
                        "note": "研究者确认采用 AI 辅助结果；仍应回查原始材料。",
                    }
                )
            project["confirmed_findings"] = confirmed
            project["last_system_action"] = f"研究者确认{WORKFLOW_TITLES[workflow_id]}结果"
            self.store.save(
                ConversationState(state.session_id, workflow_id, "after_confirm", fields, project)
            )
            next_workflow = {"w1": "w2", "w2": "w3", "w3": "w4"}.get(workflow_id)
            if next_workflow:
                return (
                    f"已记录：这份{WORKFLOW_TITLES[workflow_id]}结果由你确认采用。"
                    "这不替代对原始材料和田野语境的复核。\n\n"
                    f"接下来可以继续{WORKFLOW_TITLES[next_workflow]}，也可以回到项目主页或导出已确认成果。\n\n"
                    f"1. 继续{WORKFLOW_TITLES[next_workflow]}\n2. 返回项目主页\n3. 导出当前已确认成果"
                )
            return "已记录：这份证据质检结果由你确认采用。\n\n1. 返回项目主页\n2. 导出当前已确认成果"
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
            "source_type": "这批材料属于哪一类？\n1. 单份访谈（一个受访者的一份记录）\n2. 多份访谈\n3. 田野或观察笔记\n4. 混合材料\n\n回复编号，或直接说材料类型都可以。",
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
