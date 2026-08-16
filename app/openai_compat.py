from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from typing import Literal, Protocol
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.llm import LLMClient
from app.models import IntentRouteResult, RouteConfidence, WorkflowId
from app.multimodal.contracts import (
    ChatContent,
    FileContentPart,
    InputAudioContentPart,
    attachment_parts,
    text_from_content,
    validate_attachment_count,
)
from app.multimodal.models import Material
from app.multimodal.service import MaterialIngestService, format_material_summary
from app.routing import IntentRouter

PUBLIC_MODEL_ID = "xingxiaodao-agent"
WORKFLOW_NAMES = {
    "w1": "W1 研究设计助手",
    "w2": "W2 访谈设计助手",
    "w3": "W3 质性材料分析",
    "w4": "W4 研究质量质检",
}


class MaterialAnalysisRunner(Protocol):
    async def analyze_materials(self, materials: list[Material], research_question: str) -> str: ...


class ChatMessage(BaseModel):
    """兼容纯文本，并允许 user 消息携带 v2.2 content part。"""

    # 清小搭会在正式聊天中附带消息级兼容字段；未参与解析的字段应安全忽略。
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    role: Literal["system", "developer", "user", "assistant"]
    content: ChatContent

    @model_validator(mode="after")
    def attachment_role_and_count(self) -> ChatMessage:
        validate_attachment_count(self.content)
        if self.role != "user" and attachment_parts(self.content):
            raise ValueError("只有 user 消息可以包含附件")
        return self


class ChatCompletionRequest(BaseModel):
    # 平台可能下发 stream_options、metadata 等 OpenAI 扩展字段。
    # v2.2 不依赖它们，但不能因此拒绝已经合法的文本或附件请求。
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=50)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=16_384)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @field_validator("model")
    @classmethod
    def blank_model_is_none(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


def authorize_bearer(authorization: str | None, expected_key: str) -> None:
    """使用恒定时间比较，避免泄露密钥错误的细节。"""

    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "message": "服务端尚未配置 AGENT_API_KEY。",
                    "type": "server_configuration_error",
                    "code": "agent_key_not_configured",
                }
            },
        )
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if (
        not authorization
        or not authorization.startswith("Bearer ")
        or not secrets.compare_digest(token, expected_key)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "无效的服务访问密钥。",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
        )


def latest_user_input(
    messages: list[ChatMessage],
) -> tuple[str, list[InputAudioContentPart | FileContentPart]]:
    for message in reversed(messages):
        if message.role == "user":
            text = text_from_content(message.content)
            attachments = attachment_parts(message.content)
            if not text and attachments:
                text = "请分析这些研究材料，并进入质性材料分析流程。"
            return text, attachments
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": {
                "message": "messages 至少需要包含一条 user 消息。",
                "type": "invalid_request_error",
                "code": "missing_user_message",
            }
        },
    )


def latest_user_message(messages: list[ChatMessage]) -> str:
    """保留 v2.0 调用点；只返回最新 user 消息中的文本。"""

    return latest_user_input(messages)[0]


def format_route_reply(route: IntentRouteResult) -> str:
    if route.recommended_workflow == "uncertain":
        missing = "、".join(route.missing_information) or "你希望完成的研究任务"
        return (
            "我还不能可靠地替你选择工作流。\n\n"
            f"请补充：{missing}。\n\n"
            "你也可以直接说明：研究设计、访谈提纲、质性材料分析或研究质量质检。"
        )
    workflow_id = route.recommended_workflow
    workflow_name = WORKFLOW_NAMES[workflow_id]
    missing = "、".join(route.missing_information)
    response = f"我建议进入 **{workflow_name}**。\n\n判断依据：{route.reason}"
    if missing:
        response += f"\n\n开始前建议补充：{missing}。"
    response += "\n\n当前为标准协议接入模式；下一步请按该工作流的表单引导补充研究信息。"
    return response


async def build_reply(
    llm: LLMClient,
    request: ChatCompletionRequest,
    material_ingestor: MaterialIngestService | None = None,
    material_analyzer: MaterialAnalysisRunner | None = None,
) -> tuple[str, IntentRouteResult | None]:
    if request.max_tokens == 1:
        return "行小道服务正常。", None
    message, attachments = latest_user_input(request.messages)
    if attachments and material_ingestor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "message": "多模态输入契约已启用，但当前环境尚未配置真实材料 Provider。",
                    "type": "server_configuration_error",
                    "code": "multimodal_provider_not_configured",
                }
            },
        )
    materials = None
    if attachments:
        assert material_ingestor is not None
        materials = await material_ingestor.ingest(attachments)
    route = (
        IntentRouteResult(
            recommended_workflow=WorkflowId.w3,
            reason="当前消息携带待分析材料，按 v2.2 多模态闭环进入质性材料分析。",
            missing_information=[],
            confidence=RouteConfidence.high,
        )
        if attachments
        else await IntentRouter(llm).route(message)
    )
    reply = format_route_reply(route)
    if materials is not None:
        summary = format_material_summary(materials)
        if (
            route.recommended_workflow == "w3"
            and material_analyzer is not None
            and any(material.automatic_evidence_use for material in materials)
        ):
            report = await material_analyzer.analyze_materials(materials, message)
            reply = (
                "已进入 **W3 质性材料分析**，并完成自动可用片段的证据提取、"
                f"引文核验和主题生成。\n\n{summary}\n\n{report}"
            )
        else:
            reply += f"\n\n{summary}"
            if not any(material.automatic_evidence_use for material in materials):
                reply += "\n\n当前没有通过自动证据门控的片段；W3 未运行，请先完成人工复核。"
    return reply, route


def completion_payload(content: str, *, completion_id: str, created: int) -> dict[str, object]:
    prompt_tokens = 1
    completion_tokens = max(1, len(content) // 4)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": PUBLIC_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def stream_completion(
    content: str, *, completion_id: str, created: int
) -> AsyncIterator[str]:
    role_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": PUBLIC_MODEL_ID,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    content_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": PUBLIC_MODEL_ID,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    stop_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": PUBLIC_MODEL_ID,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": max(1, len(content) // 4),
            "total_tokens": max(2, len(content) // 4 + 1),
        },
    }
    for chunk in (role_chunk, content_chunk, stop_chunk):
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0)
    yield "data: [DONE]\n\n"


def new_completion_identity() -> tuple[str, int]:
    return f"chatcmpl-{uuid4().hex}", int(time.time())
