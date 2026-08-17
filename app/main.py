from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.blob_upload import (
    BlobGenerateTokenRequest,
    create_client_upload_token,
    delete_blob,
    validate_managed_blob_url,
)
from app.config import PROJECT_ROOT, Settings, get_settings
from app.conversation import QingxiaodaConversation
from app.llm import LLMClient, LLMError, MockLLMClient, OpenAICompatibleClient
from app.models import IntentRouteRequest, IntentRouteResult, ProjectContext
from app.multimodal.service import (
    MaterialIngestService,
    build_live_ingest_service,
    build_mock_ingest_service,
)
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


def create_app(
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    material_ingestor: MaterialIngestService | None = None,
) -> FastAPI:
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
        version="3.1.1",
        description="四工作流全代码版：OpenAI 兼容协议、项目卡串联与协作发布基线",
    )
    app.state.settings = active_settings
    app.state.store = store
    app.state.llm = active_llm
    app.state.llm_error = llm_error
    multimodal_provider = "injected" if material_ingestor is not None else "live"
    if material_ingestor is not None:
        app.state.material_ingestor = material_ingestor
    elif active_settings.app_mode == "mock":
        app.state.material_ingestor = build_mock_ingest_service()
        multimodal_provider = "mock"
    else:
        app.state.material_ingestor = build_live_ingest_service(
            max_upload_bytes=active_settings.max_upload_bytes,
            max_document_chars=active_settings.max_document_chars,
            connect_timeout=active_settings.multimodal_connect_timeout_seconds,
            read_timeout=active_settings.multimodal_read_timeout_seconds,
            max_redirects=active_settings.multimodal_max_redirects,
            asr_provider=active_settings.asr_provider,
            stepfun_asr_api_key=active_settings.stepfun_asr_api_key,
            stepfun_asr_base_url=active_settings.stepfun_asr_base_url,
            stepfun_asr_model=active_settings.stepfun_asr_model,
            stepfun_asr_request_timeout=(active_settings.stepfun_asr_request_timeout_seconds),
            stepfun_asr_poll_timeout=active_settings.stepfun_asr_poll_timeout_seconds,
            stepfun_asr_poll_interval=(active_settings.stepfun_asr_poll_interval_seconds),
            deepgram_api_key=active_settings.deepgram_api_key,
            deepgram_base_url=active_settings.deepgram_base_url,
            deepgram_model=active_settings.deepgram_model,
            deepgram_language=active_settings.deepgram_language,
            deepgram_diarize_model=active_settings.deepgram_diarize_model,
            deepgram_timeout=active_settings.deepgram_timeout_seconds,
            ocr_provider=active_settings.ocr_provider,
            baidu_ocr_api_key=active_settings.baidu_ocr_api_key,
            baidu_ocr_secret_key=active_settings.baidu_ocr_secret_key,
            baidu_ocr_base_url=active_settings.baidu_ocr_base_url,
            baidu_ocr_endpoint_path=active_settings.baidu_ocr_endpoint_path,
            baidu_ocr_timeout=active_settings.baidu_ocr_timeout_seconds,
            baidu_ocr_max_pages=active_settings.baidu_ocr_max_pages,
        )
    app.state.workflow_service = (
        WorkflowService(
            store,
            active_llm,
            active_settings,
            material_ingestor=app.state.material_ingestor,
        )
        if active_llm is not None
        else None
    )
    app.state.conversation = (
        QingxiaodaConversation(
            database_path=PROJECT_ROOT / "data" / "qingxiaoda_conversations.sqlite3",
            workflow_service=app.state.workflow_service,
            material_ingestor=app.state.material_ingestor,
        )
        if app.state.workflow_service is not None
        else None
    )
    app.state.multimodal_provider = multimodal_provider
    app.state.tasks = set()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok" if app.state.llm is not None else "configuration_required",
            "version": "3.1.1",
            "app_mode": active_settings.app_mode,
            "provider": active_settings.provider,
            "model": active_settings.model,
            "thinking": active_settings.thinking,
            "key_configured": active_settings.key_configured,
            "agent_key_configured": active_settings.agent_key_configured,
            "multimodal_contract_enabled": True,
            "multimodal_provider": app.state.multimodal_provider,
            "asr_provider": active_settings.asr_provider,
            "asr_key_configured": active_settings.asr_key_configured,
            "ocr_provider": active_settings.ocr_provider,
            "ocr_key_configured": active_settings.baidu_ocr_key_configured,
            "large_upload_configured": active_settings.blob_upload_configured,
            "max_upload_mb": active_settings.max_upload_bytes // 1024 // 1024,
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
        content, _ = await build_reply(
            app.state.llm,
            payload,
            material_ingestor=app.state.material_ingestor,
            material_analyzer=app.state.workflow_service,
            conversation=app.state.conversation,
        )
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
        source_url: str | None = Form(default=None),
        source_filename: str | None = Form(default=None),
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
        managed_source_url: str | None = None
        if source_file is not None and source_url is not None:
            raise HTTPException(status_code=400, detail="不能同时提交文件正文和大文件地址。")
        if source_url is not None:
            if not active_settings.blob_upload_configured:
                raise HTTPException(status_code=503, detail="Vercel Blob 大文件上传尚未配置。")
            filename = source_filename or "uploaded-file"
            try:
                managed_source_url = validate_managed_blob_url(source_url, filename=filename)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif source_file is not None:
            filename = source_file.filename or "uploaded-file"
            file_bytes = await source_file.read(active_settings.max_upload_bytes + 1)

        record = await store.create(workflow_id)
        service = app.state.workflow_service
        assert service is not None
        async def execute_and_cleanup() -> None:
            try:
                await service.execute(
                    record.run_id,
                    workflow_id,
                    fields,
                    filename,
                    file_bytes,
                    project_context,
                    source_url=managed_source_url,
                )
            finally:
                if (
                    managed_source_url is not None
                    and active_settings.blob_cleanup_enabled
                    and active_settings.blob_upload_configured
                ):
                    try:
                        await delete_blob(
                            managed_source_url,
                            read_write_token=active_settings.blob_read_write_token,
                        )
                    except Exception:
                        pass

        task = asyncio.create_task(execute_and_cleanup())
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        return {"run_id": record.run_id, "status": record.status}

    @app.post("/api/blob/upload-token")
    async def blob_upload_token(
        request: BlobGenerateTokenRequest,
        upload_intent: str | None = Header(default=None, alias="X-Xingxiaodao-Upload"),
    ) -> dict[str, str]:
        if upload_intent != "1":
            raise HTTPException(status_code=403, detail="缺少大文件上传意图标记。")
        if not active_settings.blob_upload_configured:
            raise HTTPException(
                status_code=503,
                detail="大文件上传尚未配置：请先为 Vercel 项目连接 Public Blob Store。",
            )
        try:
            return create_client_upload_token(
                request,
                read_write_token=active_settings.blob_read_write_token,
                max_bytes=active_settings.max_upload_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        return {"project_root": str(PROJECT_ROOT), "version": "3.1.1"}

    return app


app = create_app()
