from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm import LLMClient
from app.models import IntentRouteResult
from app.routing import IntentRouter

PUBLIC_MODEL_ID = "xingxiaodao-agent"
WORKFLOW_NAMES = {
    "w1": "W1 研究设计助手",
    "w2": "W2 访谈设计助手",
    "w3": "W3 质性材料分析",
    "w4": "W4 研究质量质检",
}


class ChatMessage(BaseModel):
    """v2.0 只支持标准字符串 content；附件 content part 留给 v2.2。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    """使用恒等失败提示，避免泄露到底是缺密钥还是密钥不正确。"""

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
    if not authorization or not authorization.startswith("Bearer ") or token != expected_key:
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


def latest_user_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
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


def format_route_reply(route: IntentRouteResult) -> str:
    if route.recommended_workflow == "uncertain":
        missing = "；".join(route.missing_information) or "你希望完成的研究任务"
        return (
            "我还不能可靠地替你选择工作流。\n\n"
            f"请补充：{missing}。\n\n"
            "你也可以直接说明：研究设计、访谈提纲、质性材料分析或研究质量质检。"
        )
    workflow_id = route.recommended_workflow
    workflow_name = WORKFLOW_NAMES[workflow_id]
    missing = "；".join(route.missing_information)
    response = f"我建议进入 **{workflow_name}**。\n\n判断依据：{route.reason}"
    if missing:
        response += f"\n\n开始前建议补充：{missing}。"
    response += "\n\n当前为标准协议接入模式；下一步请按该工作流的表单引导补充研究信息。"
    return response


async def build_reply(
    llm: LLMClient,
    request: ChatCompletionRequest,
) -> tuple[str, IntentRouteResult | None]:
    if request.max_tokens == 1:
        return "行小道服务正常。", None
    message = latest_user_message(request.messages)
    route = await IntentRouter(llm).route(message)
    return format_route_reply(route), route


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


async def stream_completion(content: str, *, completion_id: str, created: int) -> AsyncIterator[str]:
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
        "usage": {"prompt_tokens": 1, "completion_tokens": max(1, len(content) // 4), "total_tokens": max(2, len(content) // 4 + 1)},
    }
    for chunk in (role_chunk, content_chunk, stop_chunk):
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0)
    yield "data: [DONE]\n\n"


def new_completion_identity() -> tuple[str, int]:
    return f"chatcmpl-{uuid4().hex}", int(time.time())
