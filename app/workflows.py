from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.document_parser import parse_document
from app.evidence import load_json, verify_audit_evidence, verify_material_evidence
from app.llm import LLMClient
from app.models import (
    AuditExtraction,
    InterviewInput,
    MaterialAnalysisInput,
    MaterialExtraction,
    ProjectContext,
    ProjectFieldPath,
    ProjectFieldUpdate,
    ProjectMaterial,
    ProjectPatchProposal,
    ProjectStage,
    QualityAuditInput,
    ResearchDesignInput,
    ResearchDiagnosis,
    WorkflowField,
    WorkflowId,
    WorkflowSpec,
)
from app.multimodal.evidence_linking import prepare_w3_material_bundle
from app.multimodal.models import Material, MaterialSegment, MaterialStatus
from app.multimodal.service import MaterialIngestService
from app.project_prompts import PROJECT_WRITEBACK_SYSTEM, build_project_writeback_user_prompt
from app.prompts import (
    W1_DIAGNOSIS_SYSTEM,
    W1_PLAN_SYSTEM,
    W2_GENERATE_SYSTEM,
    W2_REVIEW_SYSTEM,
    W3_EXTRACT_SYSTEM,
    W3_SYNTHESIS_SYSTEM,
    W4_AUDIT_SYSTEM,
    W4_EXTRACT_SYSTEM,
    w1_diagnosis_user,
    w1_plan_user,
    w2_generate_user,
    w2_review_user,
    w3_extract_user,
    w3_synthesis_user,
    w4_audit_user,
    w4_extract_user,
)
from app.run_store import RunStore

WRITEBACK_CONFIG: dict[str, dict[str, Any]] = {
    "w1": {
        "stage": ProjectStage.w1_confirmed,
        "next": "w2",
        "allowed": {
            ProjectFieldPath.research_question,
            ProjectFieldPath.target_population,
            ProjectFieldPath.research_context,
            ProjectFieldPath.method_plan,
            ProjectFieldPath.unresolved_decisions,
        },
    },
    "w2": {
        "stage": ProjectStage.w2_confirmed,
        "next": "w3",
        "allowed": {
            ProjectFieldPath.interview_guide,
            ProjectFieldPath.unresolved_decisions,
        },
    },
    "w3": {
        "stage": ProjectStage.w3_confirmed,
        "next": "w4",
        "allowed": {
            ProjectFieldPath.materials,
            ProjectFieldPath.candidate_codes,
            ProjectFieldPath.candidate_themes,
            ProjectFieldPath.candidate_claims,
            ProjectFieldPath.unresolved_decisions,
        },
    },
    "w4": {
        "stage": ProjectStage.w4_audited,
        "next": None,
        "allowed": {
            ProjectFieldPath.audit_status,
            ProjectFieldPath.audit_notes,
            ProjectFieldPath.unresolved_decisions,
        },
    },
}

WORKFLOW_SPECS = [
    WorkflowSpec(
        id="w1",
        title="研究设计助手",
        description="把社会实践主题收敛为可执行、边界清楚的最小研究方案。",
        fields=[
            WorkflowField(name="theme", label="调研主题", kind="text", required=True),
            WorkflowField(name="purpose", label="研究目的", kind="text", required=True),
            WorkflowField(name="background", label="已有背景和假设", kind="text"),
            WorkflowField(name="deadline", label="时间限制", kind="text"),
            WorkflowField(name="participants", label="可接触对象", kind="text"),
            WorkflowField(name="resources", label="团队与资源限制", kind="text"),
        ],
    ),
    WorkflowSpec(
        id="w2",
        title="访谈设计助手",
        description="从零生成访谈提纲，或审查已有问题中的诱导和伦理风险。",
        fields=[
            WorkflowField(
                name="mode",
                label="本次任务",
                kind="select",
                required=True,
                options=[
                    {"value": "generate", "label": "从零生成访谈提纲"},
                    {"value": "review", "label": "审查已有访谈问题"},
                ],
            ),
            WorkflowField(
                name="research_question",
                label="研究问题",
                kind="text",
                required=True,
                show_when={"mode": "generate"},
            ),
            WorkflowField(
                name="participant_profile",
                label="访谈对象",
                kind="text",
                required=True,
                show_when={"mode": "generate"},
            ),
            WorkflowField(
                name="duration",
                label="预计时长",
                kind="text",
                required=True,
                show_when={"mode": "generate"},
            ),
            WorkflowField(
                name="sensitive_topics",
                label="敏感主题与限制",
                kind="text",
                show_when={"mode": "generate"},
            ),
            WorkflowField(
                name="review_topic",
                label="研究主题或问题",
                kind="text",
                show_when={"mode": "review"},
            ),
            WorkflowField(
                name="existing_questions",
                label="待审查问题",
                kind="text",
                required=True,
                show_when={"mode": "review"},
            ),
            WorkflowField(
                name="review_participant",
                label="访谈对象",
                kind="text",
                show_when={"mode": "review"},
            ),
            WorkflowField(
                name="review_requirements",
                label="特殊要求",
                kind="text",
                show_when={"mode": "review"},
            ),
        ],
    ),
    WorkflowSpec(
        id="w3",
        title="质性材料分析",
        description="对访谈与观察材料进行开放编码、引文核验和主题分析。",
        fields=[
            WorkflowField(
                name="research_question",
                label="研究问题",
                kind="text",
                required=True,
                help="填写当前材料能够回应的问题。",
            ),
            WorkflowField(
                name="source_id",
                label="材料包命名/编号",
                kind="text",
                required=True,
                help="例如 PACK-A；正文内部继续保留 I01、N01。",
            ),
            WorkflowField(
                name="source_type",
                label="材料类型",
                kind="select",
                required=True,
                options=[
                    {"value": "单份访谈", "label": "单份访谈（一个受访者的一份访谈记录）"},
                    {"value": "多份访谈", "label": "多份访谈（一个文件内含I01、I02等多份访谈）"},
                    {"value": "田野或观察笔记", "label": "田野或观察笔记（以N01、N02等编号）"},
                    {"value": "混合材料", "label": "混合材料（同时包含访谈和观察笔记）"},
                ],
            ),
            WorkflowField(
                name="source_context",
                label="材料背景（采集场景、对象范围、日期或材料限制等）",
                kind="text",
            ),
            WorkflowField(
                name="source_file",
                label="上传材料",
                kind="file",
                required=True,
                help=(
                    "单文件：文档支持 TXT、MD、DOCX、PDF；音频支持 MP3、WAV、M4A、WEBM；"
                    "图片支持 PNG、JPG、JPEG、WEBP。图片与扫描 PDF 需配置 OCR。"
                ),
                accept=(".txt,.md,.docx,.pdf,.mp3,.wav,.m4a,.webm,.png,.jpg,.jpeg,.webp"),
            ),
        ],
    ),
    WorkflowSpec(
        id="w4",
        title="研究质量质检",
        description="逐条审计研究结论的证据、反例、样本边界和推断风险。",
        fields=[
            WorkflowField(name="research_question", label="研究问题", kind="text", required=True),
            WorkflowField(
                name="candidate_claim",
                label="待审计结论（多条时用C01、C02编号）",
                kind="text",
                required=True,
            ),
            WorkflowField(
                name="target_population", label="目标研究群体", kind="text", required=True
            ),
            WorkflowField(
                name="sample_summary",
                label="实际样本概况",
                kind="text",
                required=True,
                help="人数、来源和关键特征；未知项写“未知”。",
            ),
            WorkflowField(name="source_id", label="材料包命名/编号", kind="text", required=True),
            WorkflowField(
                name="source_context",
                label="材料背景（采集场景、对象范围、日期或材料限制等）",
                kind="text",
            ),
            WorkflowField(
                name="source_file",
                label="上传原始证据材料",
                kind="file",
                required=True,
                help="不得上传人工评分答案。",
            ),
        ],
    ),
]


class WorkflowService:
    def __init__(
        self,
        store: RunStore,
        llm: LLMClient,
        settings: Settings,
        material_ingestor: MaterialIngestService | None = None,
    ) -> None:
        self.store = store
        self.llm = llm
        self.settings = settings
        self.material_ingestor = material_ingestor

    async def analyze_materials(self, materials: list[Material], research_question: str) -> str:
        """从统一多模态材料直接执行 W3，并返回最终报告。"""

        bundle = prepare_w3_material_bundle(
            materials, max_characters=self.settings.max_document_chars
        )
        fields = MaterialAnalysisInput(
            research_question=(
                research_question.strip() or "当前材料中有哪些可核验的主题、差异、反例和信息缺口？"
            ),
            source_id=bundle.source_id,
            source_type=bundle.source_type,
            source_context=bundle.source_context,
        )
        record = await self.store.create("w3")
        await self.store.set_running(record.run_id)
        try:
            input_index = await self.store.begin_node(
                record.run_id, "1I-3-1", "输入-多模态材料分析-1"
            )
            await self.store.complete_node(
                record.run_id,
                input_index,
                {
                    **fields.model_dump(),
                    "materials": [
                        {
                            "material_id": material.material_id,
                            "filename": material.filename,
                            "modality": material.modality.value,
                            "status": material.status.value,
                            "automatic_segment_count": sum(
                                segment.automatic_evidence_use for segment in material.segments
                            ),
                        }
                        for material in materials
                    ],
                    "source_text_length": bundle.character_count,
                },
            )
            return await self._run_w3_source(
                record.run_id,
                fields,
                bundle.source_text,
                material_metadata={
                    "source_id": bundle.source_id,
                    "display_name": bundle.display_name,
                    "source_type": bundle.source_type,
                    "source_context": bundle.source_context,
                    "size_bytes": 0,
                    "character_count": bundle.character_count,
                    "sha256": bundle.sha256,
                },
                segment_index=bundle.segment_index,
                project_context=None,
            )
        except Exception as exc:
            await self.store.fail(record.run_id, str(exc))
            raise

    async def execute(
        self,
        run_id: str,
        workflow_id: str,
        raw_fields: dict[str, Any],
        filename: str | None,
        file_bytes: bytes | None,
        project_context: ProjectContext | None = None,
    ) -> None:
        await self.store.set_running(run_id)
        try:
            if workflow_id == "w1":
                w1_fields = ResearchDesignInput.model_validate(raw_fields)
                await self._run_w1(run_id, w1_fields, project_context)
            elif workflow_id == "w2":
                w2_fields = InterviewInput.model_validate(raw_fields)
                self._validate_w2(w2_fields)
                await self._run_w2(run_id, w2_fields, project_context)
            elif workflow_id == "w3":
                w3_fields = MaterialAnalysisInput.model_validate(raw_fields)
                await self._run_w3(run_id, w3_fields, filename, file_bytes, project_context)
            elif workflow_id == "w4":
                w4_fields = QualityAuditInput.model_validate(raw_fields)
                await self._run_w4(run_id, w4_fields, filename, file_bytes, project_context)
            else:
                raise ValueError(f"未知工作流：{workflow_id}")
        except ValidationError as exc:
            await self.store.fail(run_id, f"输入字段校验失败：{self._validation_message(exc)}")
        except Exception as exc:
            await self.store.fail(run_id, str(exc))

    @staticmethod
    def _validation_message(exc: ValidationError) -> str:
        return "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}：{error['msg']}"
            for error in exc.errors()
        )

    @staticmethod
    def _validate_w2(fields: InterviewInput) -> None:
        if fields.mode == "generate":
            missing = [
                name
                for name in ("research_question", "participant_profile", "duration")
                if not getattr(fields, name).strip()
            ]
            if missing:
                raise ValueError(f"从零生成模式缺少必填字段：{', '.join(missing)}")
        elif not fields.existing_questions.strip():
            raise ValueError("审查已有问题模式必须填写待审查问题。")

    async def _input_node(
        self,
        run_id: str,
        node_id: str,
        name: str,
        fields: BaseModel,
        *,
        filename: str | None = None,
        file_bytes: bytes | None = None,
    ) -> str | None:
        index = await self.store.begin_node(run_id, node_id, name)
        output = fields.model_dump()
        if filename is not None:
            output["source_file"] = {"filename": filename, "size_bytes": len(file_bytes or b"")}
        try:
            source_text = None
            if filename is not None:
                if file_bytes is None:
                    raise ValueError("没有收到上传文件。")
                source_text = parse_document(
                    filename,
                    file_bytes,
                    max_upload_bytes=self.settings.max_upload_bytes,
                    max_document_chars=self.settings.max_document_chars,
                )
                output["source_text_length"] = len(source_text)
                output["source_text_preview"] = source_text[:1000]
            await self.store.complete_node(run_id, index, output)
            return source_text
        except Exception as exc:
            await self.store.fail_node(run_id, index, str(exc))
            raise

    async def _llm_node(
        self,
        run_id: str,
        node_id: str,
        name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        json_model: type[BaseModel] | None = None,
    ) -> str:
        index = await self.store.begin_node(
            run_id,
            node_id,
            name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        try:
            output = await self.llm.complete(
                node_id=node_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                json_model=json_model,
            )
            await self.store.complete_node(run_id, index, output)
            return output
        except Exception as exc:
            await self.store.fail_node(run_id, index, str(exc))
            raise

    async def _code_node(
        self, run_id: str, node_id: str, name: str, operation: Any
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        index = await self.store.begin_node(run_id, node_id, name)
        try:
            verified, rejected = operation()
            await self.store.complete_node(
                run_id,
                index,
                {
                    "verification_status": "verified",
                    "verified_evidence_json": verified,
                    "rejected_quotes_json": rejected,
                },
            )
            return verified, rejected
        except Exception as exc:
            await self.store.fail_node(run_id, index, str(exc))
            raise

    async def _project_writeback_node(
        self,
        *,
        run_id: str,
        workflow_id: str,
        workflow_input: dict[str, Any],
        final_markdown: str,
        project_context: ProjectContext | None,
        structured_result: dict[str, Any] | None = None,
    ) -> ProjectPatchProposal:
        config = WRITEBACK_CONFIG[workflow_id]
        node_id = f"3L-{workflow_id[1:]}-3"
        user_prompt = build_project_writeback_user_prompt(
            workflow_id=WorkflowId(workflow_id),
            stage_after_confirmation=config["stage"].value,
            next_workflow=config["next"],
            allowed_fields=sorted(path.value for path in config["allowed"]),
            project_context=(
                project_context.model_dump(mode="json") if project_context is not None else None
            ),
            workflow_input=workflow_input,
            final_markdown=final_markdown,
            structured_result=structured_result,
        )
        index = await self.store.begin_node(
            run_id,
            node_id,
            "大模型-项目卡写回建议-3",
            system_prompt=PROJECT_WRITEBACK_SYSTEM,
            user_prompt=user_prompt,
        )
        try:
            output = await self.llm.complete(
                node_id=node_id,
                system_prompt=PROJECT_WRITEBACK_SYSTEM,
                user_prompt=user_prompt,
                temperature=0,
                json_model=ProjectPatchProposal,
            )
            proposal = ProjectPatchProposal.model_validate(load_json(output))
            if (
                workflow_id == "w3"
                and structured_result
                and structured_result.get("material_metadata")
            ):
                proposal.updates = [
                    update
                    for update in proposal.updates
                    if update.path != ProjectFieldPath.materials
                ]
                proposal.updates.append(
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.materials,
                        proposed_value=[
                            ProjectMaterial.model_validate(structured_result["material_metadata"])
                        ],
                        reason="由后端根据本次上传文件生成的材料元数据，不含原文。",
                    )
                )
            self._validate_writeback(workflow_id, proposal)
            await self.store.complete_node(run_id, index, proposal.model_dump(mode="json"))
            return proposal
        except Exception as exc:
            await self.store.fail_node(run_id, index, str(exc))
            return self._fallback_writeback(
                workflow_id=workflow_id,
                workflow_input=workflow_input,
                final_markdown=final_markdown,
                structured_result=structured_result or {},
                warning=f"项目卡结构化建议生成失败，已使用保守降级结果：{exc}",
            )

    @staticmethod
    def _validate_writeback(workflow_id: str, proposal: ProjectPatchProposal) -> None:
        config = WRITEBACK_CONFIG[workflow_id]
        if proposal.workflow_id.value != workflow_id:
            raise ValueError("项目卡建议的 workflow_id 与当前工作流不一致。")
        if proposal.stage_after_confirmation != config["stage"]:
            raise ValueError("项目卡建议返回了错误的阶段。")
        actual_next = proposal.next_workflow.value if proposal.next_workflow else None
        if actual_next != config["next"]:
            raise ValueError("项目卡建议返回了错误的下一工作流。")
        paths = [update.path for update in proposal.updates]
        if len(paths) != len(set(paths)):
            raise ValueError("项目卡建议包含重复字段。")
        if any(path not in config["allowed"] for path in paths):
            raise ValueError("项目卡建议试图写入当前工作流无权修改的字段。")
        if len(json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False)) > 200_000:
            raise ValueError("项目卡建议内容过长。")

    @staticmethod
    def _fallback_writeback(
        *,
        workflow_id: str,
        workflow_input: dict[str, Any],
        final_markdown: str,
        structured_result: dict[str, Any],
        warning: str,
    ) -> ProjectPatchProposal:
        updates: list[ProjectFieldUpdate] = []
        if workflow_id == "w1":
            context = "\n\n".join(
                value
                for value in (
                    workflow_input.get("theme", ""),
                    workflow_input.get("background", ""),
                )
                if value
            )
            if context:
                updates.append(
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.research_context,
                        proposed_value=context,
                        reason="来自本次研究设计输入。",
                    )
                )
            if workflow_input.get("participants"):
                updates.append(
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.target_population,
                        proposed_value=workflow_input["participants"],
                        reason="来自本次可接触对象输入。",
                    )
                )
            updates.append(
                ProjectFieldUpdate(
                    path=ProjectFieldPath.method_plan,
                    proposed_value=final_markdown[:50_000],
                    reason="保守保存本次研究设计结果，等待用户确认。",
                )
            )
        elif workflow_id == "w2":
            updates.append(
                ProjectFieldUpdate(
                    path=ProjectFieldPath.interview_guide,
                    proposed_value=final_markdown[:50_000],
                    reason="保存本次访谈设计或审查结果。",
                )
            )
        elif workflow_id == "w3":
            material = structured_result.get("material_metadata")
            if material:
                updates.append(
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.materials,
                        proposed_value=[ProjectMaterial.model_validate(material)],
                        reason="保存本次上传材料的元数据，不含原文。",
                    )
                )
            extracted = structured_result.get("extracted", {})
            codes = [item.get("label", "") for item in extracted.get("open_codes", [])]
            codes = [item for item in codes if item]
            if codes:
                updates.append(
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.candidate_codes,
                        proposed_value=codes[:200],
                        reason="来自已结构化提取的开放编码标签。",
                    )
                )
        else:
            updates.extend(
                [
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.audit_status,
                        proposed_value="已完成研究质量质检，等待人工确认。",
                        reason="本次 W4 已完成。",
                    ),
                    ProjectFieldUpdate(
                        path=ProjectFieldPath.audit_notes,
                        proposed_value=final_markdown[:50_000],
                        reason="保守保存本次质检结果。",
                    ),
                ]
            )
        config = WRITEBACK_CONFIG[workflow_id]
        return ProjectPatchProposal(
            workflow_id=WorkflowId(workflow_id),
            updates=updates,
            stage_after_confirmation=config["stage"],
            next_workflow=config["next"],
            warning=warning,
        )

    async def _finish(
        self,
        run_id: str,
        output_node: str,
        title: str,
        markdown: str,
        project_patch: ProjectPatchProposal | None,
    ) -> None:
        output_index = await self.store.begin_node(run_id, output_node, title)
        await self.store.complete_node(
            run_id,
            output_index,
            {
                "final_markdown": markdown,
                "actions": ["重新使用当前助手", "返回主菜单", "导出Markdown", "直接结束"],
            },
        )
        end_node = output_node.replace("2O", "9E")
        end_index = await self.store.begin_node(run_id, end_node, "结束")
        await self.store.complete_node(run_id, end_index, {"ended": True})
        await self.store.succeed(run_id, markdown, project_patch)

    async def _run_w1(
        self,
        run_id: str,
        fields: ResearchDesignInput,
        project_context: ProjectContext | None,
    ) -> None:
        await self._input_node(run_id, "1I-1-1", "输入-研究设计-1", fields)
        raw = fields.model_dump()
        diagnosis_user = w1_diagnosis_user(raw)
        diagnosis = await self._llm_node(
            run_id,
            "3L-1-1",
            "大模型-研究设计诊断-1",
            W1_DIAGNOSIS_SYSTEM,
            diagnosis_user,
            0.1,
            ResearchDiagnosis,
        )
        plan_user = w1_plan_user(raw, diagnosis)
        markdown = await self._llm_node(
            run_id,
            "3L-1-2",
            "大模型-研究方案生成-2",
            W1_PLAN_SYSTEM,
            plan_user,
            0.2,
        )
        patch = (
            await self._project_writeback_node(
                run_id=run_id,
                workflow_id="w1",
                workflow_input=raw,
                final_markdown=markdown,
                project_context=project_context,
                structured_result={"diagnosis": load_json(diagnosis)},
            )
            if project_context is not None
            else None
        )
        await self._finish(run_id, "2O-1-1", "输出-研究设计-1", markdown, patch)

    async def _run_w2(
        self,
        run_id: str,
        fields: InterviewInput,
        project_context: ProjectContext | None,
    ) -> None:
        route_index = await self.store.begin_node(run_id, "2O-2-1", "输出-访谈任务选择-1")
        await self.store.complete_node(run_id, route_index, {"mode": fields.mode})
        raw = fields.model_dump()
        draft = ""
        if fields.mode == "generate":
            await self._input_node(run_id, "1I-2-1", "输入-访谈生成-1", fields)
            draft = await self._llm_node(
                run_id,
                "3L-2-1",
                "大模型-提纲生成-1",
                W2_GENERATE_SYSTEM,
                w2_generate_user(raw),
                0.2,
            )
        else:
            await self._input_node(run_id, "1I-2-2", "输入-已有问题审查-2", fields)

        markdown = await self._llm_node(
            run_id,
            "3L-2-2",
            "大模型-风险审查-2",
            W2_REVIEW_SYSTEM,
            w2_review_user(raw, draft),
            0.1,
        )
        patch = (
            await self._project_writeback_node(
                run_id=run_id,
                workflow_id="w2",
                workflow_input=raw,
                final_markdown=markdown,
                project_context=project_context,
                structured_result={"generated_draft": draft},
            )
            if project_context is not None
            else None
        )
        await self._finish(run_id, "2O-2-2", "输出-访谈设计-2", markdown, patch)

    async def _run_w3(
        self,
        run_id: str,
        fields: MaterialAnalysisInput,
        filename: str | None,
        file_bytes: bytes | None,
        project_context: ProjectContext | None,
    ) -> None:
        suffix = (
            PurePosixPath(filename.replace("\\", "/")).suffix.lower()
            if filename is not None
            else ""
        )
        multimodal_suffixes = {
            ".txt",
            ".md",
            ".docx",
            ".pdf",
            ".mp3",
            ".wav",
            ".m4a",
            ".webm",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }
        if self.material_ingestor is not None and suffix in multimodal_suffixes:
            assert filename is not None
            await self._run_w3_material_upload(
                run_id,
                fields,
                filename,
                file_bytes,
                project_context,
            )
            return
        source_text = await self._input_node(
            run_id,
            "1I-3-1",
            "输入-材料分析-1",
            fields,
            filename=filename,
            file_bytes=file_bytes,
        )
        if source_text is None:
            raise ValueError("没有取得材料正文。")
        material_metadata = {
            "source_id": fields.source_id,
            "display_name": filename or fields.source_id,
            "source_type": fields.source_type,
            "source_context": fields.source_context,
            "size_bytes": len(file_bytes or b""),
            "character_count": len(source_text),
            "sha256": hashlib.sha256(file_bytes or b"").hexdigest(),
        }
        await self._run_w3_source(
            run_id,
            fields,
            source_text,
            material_metadata=material_metadata,
            segment_index=None,
            project_context=project_context,
        )

    async def _run_w3_material_upload(
        self,
        run_id: str,
        fields: MaterialAnalysisInput,
        filename: str,
        file_bytes: bytes | None,
        project_context: ProjectContext | None,
    ) -> None:
        index = await self.store.begin_node(run_id, "1I-3-1", "输入-多模态材料分析-1")
        try:
            if file_bytes is None:
                raise ValueError("没有收到上传材料。")
            assert self.material_ingestor is not None
            material = await self.material_ingestor.ingest_upload(filename, file_bytes)
            if material.status is MaterialStatus.failed:
                messages = "；".join(issue.message for issue in material.issues)
                raise ValueError(f"材料处理失败：{messages or '识别服务没有返回可用内容。'}")
            bundle = prepare_w3_material_bundle(
                [material], max_characters=self.settings.max_document_chars
            )
            await self.store.complete_node(
                run_id,
                index,
                {
                    **fields.model_dump(),
                    "source_file": {"filename": filename, "size_bytes": len(file_bytes)},
                    "material": {
                        "material_id": material.material_id,
                        "modality": material.modality.value,
                        "status": material.status.value,
                        "provider_name": material.provider_name,
                        "provider_model": material.provider_model,
                        "segment_count": len(material.segments),
                        "automatic_segment_count": sum(
                            segment.automatic_evidence_use for segment in material.segments
                        ),
                    },
                    "source_text_length": bundle.character_count,
                },
            )
        except Exception as exc:
            await self.store.fail_node(run_id, index, str(exc))
            raise

        await self._run_w3_source(
            run_id,
            fields,
            bundle.source_text,
            material_metadata={
                "source_id": fields.source_id,
                "display_name": filename,
                "source_type": fields.source_type,
                "source_context": fields.source_context,
                "size_bytes": len(file_bytes),
                "character_count": bundle.character_count,
                "sha256": material.source_fingerprint,
                "material_id": material.material_id,
                "provider_name": material.provider_name,
                "provider_model": material.provider_model,
            },
            segment_index=bundle.segment_index,
            project_context=project_context,
        )

    async def _run_w3_source(
        self,
        run_id: str,
        fields: MaterialAnalysisInput,
        source_text: str,
        *,
        material_metadata: dict[str, Any],
        segment_index: dict[str, MaterialSegment] | None,
        project_context: ProjectContext | None,
    ) -> str:
        raw = fields.model_dump()
        extraction_json = await self._llm_node(
            run_id,
            "3L-3-1",
            "大模型-证据提取-1",
            W3_EXTRACT_SYSTEM,
            w3_extract_user(raw, source_text),
            0.1,
            MaterialExtraction,
        )
        extracted = load_json(extraction_json)
        verified, rejected = await self._code_node(
            run_id,
            "7C-3-1",
            "代码-引文核验-1",
            lambda: verify_material_evidence(extracted, source_text, segment_index),
        )
        markdown = await self._llm_node(
            run_id,
            "3L-3-2",
            "大模型-主题生成-2",
            W3_SYNTHESIS_SYSTEM,
            w3_synthesis_user(raw, verified, rejected),
            0.1,
        )
        material_metadata["summary"] = str(extracted.get("material_summary", ""))[:4000]
        patch = (
            await self._project_writeback_node(
                run_id=run_id,
                workflow_id="w3",
                workflow_input=raw,
                final_markdown=markdown,
                project_context=project_context,
                structured_result={
                    "extracted": extracted,
                    "verified": verified,
                    "rejected": rejected,
                    "material_metadata": material_metadata,
                },
            )
            if project_context is not None
            else None
        )
        await self._finish(run_id, "2O-3-1", "输出-材料分析-1", markdown, patch)
        return markdown

    async def _run_w4(
        self,
        run_id: str,
        fields: QualityAuditInput,
        filename: str | None,
        file_bytes: bytes | None,
        project_context: ProjectContext | None,
    ) -> None:
        source_text = await self._input_node(
            run_id,
            "1I-4-1",
            "输入-质量质检-1",
            fields,
            filename=filename,
            file_bytes=file_bytes,
        )
        if source_text is None:
            raise ValueError("没有取得证据正文。")
        raw = fields.model_dump()
        extraction_json = await self._llm_node(
            run_id,
            "3L-4-1",
            "大模型-质检证据提取-1",
            W4_EXTRACT_SYSTEM,
            w4_extract_user(raw, source_text),
            0.1,
            AuditExtraction,
        )
        extracted = load_json(extraction_json)
        verified, rejected = await self._code_node(
            run_id,
            "7C-4-1",
            "代码-质检引文核验-1",
            lambda: verify_audit_evidence(extracted, source_text),
        )
        markdown = await self._llm_node(
            run_id,
            "3L-4-2",
            "大模型-研究质量审计-2",
            W4_AUDIT_SYSTEM,
            w4_audit_user(raw, verified, rejected),
            0.1,
        )
        patch = (
            await self._project_writeback_node(
                run_id=run_id,
                workflow_id="w4",
                workflow_input=raw,
                final_markdown=markdown,
                project_context=project_context,
                structured_result={
                    "extracted": extracted,
                    "verified": verified,
                    "rejected": rejected,
                    "material_metadata": {
                        "source_id": fields.source_id,
                        "display_name": filename or fields.source_id,
                        "source_type": "质检证据材料",
                        "source_context": fields.source_context,
                        "size_bytes": len(file_bytes or b""),
                        "character_count": len(source_text),
                        "sha256": hashlib.sha256(file_bytes or b"").hexdigest(),
                    },
                },
            )
            if project_context is not None
            else None
        )
        await self._finish(run_id, "2O-4-1", "输出-质量质检-1", markdown, patch)


def workflow_specs_json() -> list[dict[str, Any]]:
    return [item.model_dump() for item in WORKFLOW_SPECS]
