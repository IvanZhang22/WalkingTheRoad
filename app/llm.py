from __future__ import annotations

import json
from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.evidence import clean_json_text

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        node_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        json_model: type[T] | None = None,
    ) -> str: ...


class OpenAICompatibleClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.key_configured:
            raise LLMError(
                "尚未配置 MODEL_API_KEY，请先填写项目根目录的 .env 中当前启用服务商的 Key。"
            )
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=2,
        )

    async def complete(
        self,
        *,
        node_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        json_model: type[T] | None = None,
    ) -> str:
        kwargs: dict[str, object] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if self.settings.provider == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": self.settings.thinking}}
        if json_model is not None:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await self.client.chat.completions.create(**kwargs)  # type: ignore[call-overload]
            content = response.choices[0].message.content or ""
        except Exception as exc:
            raise LLMError(f"{node_id} 调用模型失败：{exc}") from exc

        if not content.strip():
            raise LLMError(f"{node_id} 返回了空内容。")
        if json_model is None:
            return content

        try:
            return self._validate_json(content, json_model)
        except (json.JSONDecodeError, ValidationError, TypeError) as first_error:
            return await self._repair_json(
                node_id=node_id,
                invalid_content=content,
                model=json_model,
                first_error=first_error,
            )

    @staticmethod
    def _validate_json(content: str, model: type[T]) -> str:
        data = json.loads(clean_json_text(content))
        validated = model.model_validate(data)
        return validated.model_dump_json(indent=2)

    async def _repair_json(
        self,
        *,
        node_id: str,
        invalid_content: str,
        model: type[T],
        first_error: Exception,
    ) -> str:
        repair_system = (
            "你是JSON修复器。只修复格式和字段结构，不增加新的事实。"
            "只输出一个有效JSON对象，不要输出代码围栏或解释。"
        )
        repair_user = (
            f"目标JSON Schema：\n{json.dumps(model.model_json_schema(), ensure_ascii=False)}\n\n"
            f"首次错误：\n{first_error}\n\n待修复内容：\n{invalid_content}"
        )
        try:
            repair_kwargs: dict[str, object] = {
                "model": self.settings.model,
                "messages": [
                    {"role": "system", "content": repair_system},
                    {"role": "user", "content": repair_user},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            if self.settings.provider == "deepseek":
                repair_kwargs["extra_body"] = {"thinking": {"type": self.settings.thinking}}
            response = await self.client.chat.completions.create(**repair_kwargs)  # type: ignore[call-overload]
            repaired = response.choices[0].message.content or ""
            return self._validate_json(repaired, model)
        except Exception as exc:
            raise LLMError(f"{node_id} 的结构化输出校验失败；自动修复一次后仍无效：{exc}") from exc


class MockLLMClient:
    """自动测试使用的可预测模型，不会在 live 失败时自动启用。"""

    async def complete(
        self,
        *,
        node_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        json_model: type[T] | None = None,
    ) -> str:
        del system_prompt, temperature, json_model
        if node_id == "3L-0-1":
            return IntentRouteMock.json(user_prompt)
        if node_id == "3L-1-1":
            return ResearchDiagnosisMock.json()
        if node_id == "3L-3-1":
            return MaterialExtractionMock.json()
        if node_id == "3L-4-1":
            return AuditExtractionMock.json()
        if node_id in {"3L-1-3", "3L-2-3", "3L-3-3", "3L-4-3"}:
            return ProjectWritebackMock.json(node_id)
        return f"# {node_id} 模拟结果\n\n该结果仅用于自动测试，不是正式研究建议。"


class ResearchDiagnosisMock:
    @staticmethod
    def json() -> str:
        keys = [
            "scope_problems",
            "operationalization",
            "participant_fit",
            "method_fit",
            "time_and_resource_risks",
            "known_facts",
            "provisional_assumptions",
            "decisions_needed",
        ]
        return json.dumps({key: [] for key in keys}, ensure_ascii=False)


class IntentRouteMock:
    @staticmethod
    def json(user_prompt: str) -> str:
        text = user_prompt.lower()
        rules: list[tuple[str, tuple[str, ...]]] = [
            ("w4", ("质检", "审查结论", "证据是否", "是否可靠", "报告审查", "夸大")),
            ("w3", ("访谈记录", "观察笔记", "质性材料", "材料分析", "开放编码", "主题分析")),
            ("w2", ("访谈提纲", "访谈问题", "问题审查", "诱导性问题", "设计问题")),
            ("w1", ("研究设计", "调研方案", "研究问题", "研究对象", "研究方法", "选题")),
        ]
        matches: list[tuple[int, str]] = []
        for candidate, keywords in rules:
            positions = [text.find(word) for word in keywords if word in text]
            if positions:
                matches.append((min(positions), candidate))
        matches.sort()
        workflow = matches[0][1] if matches else "uncertain"
        if workflow == "uncertain":
            result = {
                "recommended_workflow": "uncertain",
                "reason": "当前描述不足以判断用户已有何种材料及希望完成的研究阶段。",
                "missing_information": ["请说明你已有的材料，以及希望得到什么结果。"],
                "confidence": "low",
                "possible_secondary_workflow": None,
            }
        else:
            labels = {
                "w1": "研究方案仍需界定",
                "w2": "当前任务是生成或审查访谈问题",
                "w3": "当前任务是分析已有质性原始材料",
                "w4": "当前任务是核查已有结论或报告",
            }
            result = {
                "recommended_workflow": workflow,
                "reason": labels[workflow],
                "missing_information": [],
                "confidence": "high",
                "possible_secondary_workflow": matches[1][1] if len(matches) > 1 else None,
            }
        return json.dumps(result, ensure_ascii=False)


class ProjectWritebackMock:
    @staticmethod
    def json(node_id: str) -> str:
        workflow = f"w{node_id.split('-')[1]}"
        configs: dict[str, dict[str, object]] = {
            "w1": {
                "updates": [
                    {
                        "path": "research_question",
                        "proposed_value": "模拟研究问题",
                        "reason": "模拟结构化写回。",
                    },
                    {
                        "path": "method_plan",
                        "proposed_value": "模拟研究方案",
                        "reason": "模拟结构化写回。",
                    },
                ],
                "stage": "w1_confirmed",
                "next": "w2",
            },
            "w2": {
                "updates": [
                    {
                        "path": "interview_guide",
                        "proposed_value": "模拟访谈提纲",
                        "reason": "模拟结构化写回。",
                    }
                ],
                "stage": "w2_confirmed",
                "next": "w3",
            },
            "w3": {
                "updates": [
                    {
                        "path": "candidate_codes",
                        "proposed_value": ["模拟编码"],
                        "reason": "模拟结构化写回。",
                    },
                    {
                        "path": "candidate_themes",
                        "proposed_value": ["模拟主题"],
                        "reason": "模拟结构化写回。",
                    },
                ],
                "stage": "w3_confirmed",
                "next": "w4",
            },
            "w4": {
                "updates": [
                    {
                        "path": "audit_status",
                        "proposed_value": "模拟质检已完成",
                        "reason": "模拟结构化写回。",
                    }
                ],
                "stage": "w4_audited",
                "next": None,
            },
        }
        config = configs[workflow]
        return json.dumps(
            {
                "workflow_id": workflow,
                "updates": config["updates"],
                "stage_after_confirmation": config["stage"],
                "next_workflow": config["next"],
                "missing_prerequisites": [],
                "warning": "",
            },
            ensure_ascii=False,
        )


class MaterialExtractionMock:
    @staticmethod
    def json() -> str:
        return json.dumps(
            {
                "material_summary": "模拟材料摘要",
                "source_ids": [],
                "open_codes": [],
                "evidence": [],
                "contrasts": [],
                "uncertainties": [],
            },
            ensure_ascii=False,
        )


class AuditExtractionMock:
    @staticmethod
    def json() -> str:
        return json.dumps(
            {
                "claims": [],
                "sample_check": {
                    "target_population": "",
                    "sample_summary": "",
                    "coverage_gaps": [],
                    "status": "信息不足",
                },
            },
            ensure_ascii=False,
        )
