from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings, get_settings
from app.llm import LLMClient, LLMError, MockLLMClient, OpenAICompatibleClient
from app.models import IntentRouteRequest, IntentRouteResult, ProjectContext
from app.openai_compat import (
    PUBLIC_MODEL_ID,
    ChatCompletionRequest,
    authorize_bearer,
    build_reply,
    completion_payload,
    new_completion_identity,
    stream_completion,
)
from app.routing import IntentRouter
from app.run_store import RunStore
from app.workflows import WorkflowService, workflow_specs_json

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Utf8JSONResponse(JSONResponse):
    """Explicit UTF-8 JSON for legacy HTTP clients."""

    media_type = "application/json; charset=utf-8"


def _make_llm(settings: Settings) -> LLMClient:
    if settings.app_mode == "mock":
        return MockLLMClient()
    return OpenAICompatibleClient(settings)


def create_app(*, settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    store = RunStore()
    active_llm = llm
    llm_error: str | None = None
    if active_llm is None:
        try:
            active_llm = _make_llm(active_settings)
        except Exception as exc:
            llm_error = str(exc)

    app = FastAPI(
        default_response_class=Utf8JSONResponse,
        title="行小道本地 Agent",
        version="2.0.0",
        description="四工作流全代码版：OpenAI 兼容协议、项目卡串联与协作发布基线",
    )
    app.state.settings = active_settings
    app.state.store = store
    app.state.llm = active_llm
    app.state.llm_error = llm_error
    app.state.tasks = set()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok" if app.state.llm is not None else "configuration_required",
            "version": "2.0.0",
            "app_mode": active_settings.app_mode,
            "provider": active_settings.provider,
            "model": active_settings.model,
            "thinking": active_settings.thinking,
            "key_configured": active_settings.key_configured,
            "agent_key_configured": active_settings.agent_key_configured,
            "configuration_error": app.state.llm_error,
        }

    @app.get("/api/workflows")
    async def list_workflows() -> list[dict[str, Any]]:
        return workflow_specs_json()

    @app.get("/v1/models")
    async def openai_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize_bearer(authorization, active_settings.agent_api_key)
        return {
            "object": "list",
            "data": [
                {
                    "id": PUBLIC_MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": "xingxiaodao",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def openai_chat_completions(
        payload: ChatCompletionRequest,
        authorization: str | None = Header(default=None),
    ) -> Any:
        authorize_bearer(authorization, active_settings.agent_api_key)
        if app.state.llm is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "message": app.state.llm_error or "模型客户端未配置。",
                        "type": "server_error",
                        "code": "model_unavailable",
                    }
                },
            )
        completion_id, created = new_completion_identity()
        content, _ = await build_reply(app.state.llm, payload)
        if payload.stream:
            return StreamingResponse(
                stream_completion(content, completion_id=completion_id, created=created),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return completion_payload(content, completion_id=completion_id, created=created)

    @app.post("/api/route", response_model=IntentRouteResult)
    async def route_intent(payload: IntentRouteRequest) -> IntentRouteResult:
        if app.state.llm is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=app.state.llm_error or "模型客户端未配置。",
            )
        try:
            return await IntentRouter(app.state.llm).route(payload.message)
        except (LLMError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"意图识别失败：{exc}",
            ) from exc

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        workflow_id: str = Form(...),
        fields_json: str = Form(...),
        project_context_json: str | None = Form(default=None),
        source_file: UploadFile | None = File(default=None),
    ) -> dict[str, str]:
        if app.state.llm is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=app.state.llm_error or "模型客户端未配置。",
            )
        known_ids = {item["id"] for item in workflow_specs_json()}
        if workflow_id not in known_ids:
            raise HTTPException(status_code=400, detail=f"未知工作流：{workflow_id}")
        if len(fields_json) > 100_000:
            raise HTTPException(status_code=400, detail="输入字段过长。")
        try:
            fields = json.loads(fields_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"fields_json 不是有效 JSON：{exc}"
            ) from exc
        if not isinstance(fields, dict):
            raise HTTPException(status_code=400, detail="fields_json 顶层必须是对象。")

        project_context: ProjectContext | None = None
        if project_context_json:
            if len(project_context_json) > 250_000:
                raise HTTPException(status_code=400, detail="项目卡上下文过长。")
            try:
                project_context = ProjectContext.model_validate_json(project_context_json)
            except (ValidationError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"项目卡上下文无效：{exc}") from exc

        filename: str | None = None
        file_bytes: bytes | None = None
        if source_file is not None:
            filename = source_file.filename or "uploaded-file"
            file_bytes = await source_file.read(active_settings.max_upload_bytes + 1)

        record = await store.create(workflow_id)
        service = WorkflowService(store, app.state.llm, active_settings)
        task = asyncio.create_task(
            service.execute(
                record.run_id,
                workflow_id,
                fields,
                filename,
                file_bytes,
                project_context,
            )
        )
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        return {"run_id": record.run_id, "status": record.status}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        record = await store.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="运行记录不存在或已被清理。")
        return record.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/download.md")
    async def download_markdown(run_id: str) -> PlainTextResponse:
        record = await store.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="运行记录不存在或已被清理。")
        if not record.final_markdown:
            raise HTTPException(status_code=409, detail="当前运行尚无可导出的最终结果。")
        filename = f"xingxiaodao_{record.workflow_id}_{run_id[:8]}.md"
        return PlainTextResponse(
            record.final_markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/project")
    async def project_info() -> dict[str, str]:
        return {"project_root": str(PROJECT_ROOT), "version": "2.0.0"}

    return app


app = create_app()
