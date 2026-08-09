from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class NodeStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowId(StrEnum):
    w1 = "w1"
    w2 = "w2"
    w3 = "w3"
    w4 = "w4"


class RouteConfidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class ProjectStage(StrEnum):
    draft = "draft"
    w1_confirmed = "w1_confirmed"
    w2_confirmed = "w2_confirmed"
    materials_ready = "materials_ready"
    w3_confirmed = "w3_confirmed"
    w4_audited = "w4_audited"


class ProjectFieldPath(StrEnum):
    research_question = "research_question"
    target_population = "target_population"
    research_context = "research_context"
    method_plan = "method_plan"
    interview_guide = "interview_guide"
    materials = "materials"
    candidate_codes = "candidate_codes"
    candidate_themes = "candidate_themes"
    candidate_claims = "candidate_claims"
    audit_status = "audit_status"
    audit_notes = "audit_notes"
    unresolved_decisions = "unresolved_decisions"


class ProjectMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=300)
    source_type: str = Field(default="", max_length=100)
    source_context: str = Field(default="", max_length=4000)
    size_bytes: int = Field(default=0, ge=0)
    character_count: int = Field(default=0, ge=0)
    sha256: str = Field(default="", max_length=64)
    summary: str = Field(default="", max_length=4000)


class ProjectContext(BaseModel):
    """从浏览器项目卡传入后端的最小、无原文上下文。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    project_id: str = Field(min_length=1, max_length=100)
    project_name: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=0, ge=0)
    research_question: str = Field(default="", max_length=20_000)
    target_population: str = Field(default="", max_length=10_000)
    research_context: str = Field(default="", max_length=20_000)
    method_plan: str = Field(default="", max_length=50_000)
    interview_guide: str = Field(default="", max_length=50_000)
    materials: list[ProjectMaterial] = Field(default_factory=list, max_length=50)
    candidate_codes: list[str] = Field(default_factory=list, max_length=200)
    candidate_themes: list[str] = Field(default_factory=list, max_length=100)
    candidate_claims: list[str] = Field(default_factory=list, max_length=100)
    audit_status: str = Field(default="", max_length=10_000)
    audit_notes: str = Field(default="", max_length=50_000)
    current_stage: ProjectStage = ProjectStage.draft
    unresolved_decisions: list[str] = Field(default_factory=list, max_length=100)


class ProjectFieldUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: ProjectFieldPath
    proposed_value: str | list[str] | list[ProjectMaterial]
    reason: str = Field(min_length=1, max_length=2000)


class ProjectPatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: WorkflowId
    updates: list[ProjectFieldUpdate] = Field(default_factory=list, max_length=20)
    stage_after_confirmation: ProjectStage
    next_workflow: WorkflowId | None = None
    missing_prerequisites: list[str] = Field(default_factory=list, max_length=20)
    warning: str = Field(default="", max_length=2000)


class IntentRouteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class IntentRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_workflow: WorkflowId | Literal["uncertain"]
    reason: str = Field(min_length=1)
    missing_information: list[str] = Field(default_factory=list)
    confidence: RouteConfidence
    possible_secondary_workflow: WorkflowId | None = None


class NodeTrace(BaseModel):
    legacy_node_id: str
    internal_name: str
    status: NodeStatus = NodeStatus.running
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    duration_ms: int | None = None
    system_prompt: str = ""
    user_prompt: str = ""
    output: Any = None
    error: str | None = None


class RunRecord(BaseModel):
    run_id: str
    workflow_id: str
    status: RunStatus = RunStatus.queued
    current_node: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    traces: list[NodeTrace] = Field(default_factory=list)
    final_markdown: str | None = None
    proposed_project_patch: ProjectPatchProposal | None = None
    error: str | None = None


class ResearchDesignInput(BaseModel):
    theme: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    background: str = ""
    deadline: str = ""
    participants: str = ""
    resources: str = ""


class InterviewInput(BaseModel):
    mode: Literal["generate", "review"]
    research_question: str = ""
    participant_profile: str = ""
    duration: str = ""
    sensitive_topics: str = ""
    review_topic: str = ""
    existing_questions: str = ""
    review_participant: str = ""
    review_requirements: str = ""


class MaterialAnalysisInput(BaseModel):
    research_question: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_type: Literal["单份访谈", "多份访谈", "田野或观察笔记", "混合材料"]
    source_context: str = ""


class QualityAuditInput(BaseModel):
    research_question: str = Field(min_length=1)
    candidate_claim: str = Field(min_length=1)
    target_population: str = Field(min_length=1)
    sample_summary: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_context: str = ""


class ResearchDiagnosis(BaseModel):
    scope_problems: list[str]
    operationalization: list[str]
    participant_fit: list[str]
    method_fit: list[str]
    time_and_resource_risks: list[str]
    known_facts: list[str]
    provisional_assumptions: list[str]
    decisions_needed: list[str]


class OpenCode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code_id: str
    label: str
    meaning: str
    type: str


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    evidence_id: str
    source_id: str
    code_id: str = ""
    quote: str
    context: str
    support_type: str


class MaterialExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    material_summary: str
    source_ids: list[str]
    open_codes: list[OpenCode]
    evidence: list[EvidenceItem]
    contrasts: list[str]
    uncertainties: list[str]


class RiskTerm(BaseModel):
    term: str
    risk_type: str
    reason: str


class AuditEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    evidence_id: str
    source_id: str
    quote: str
    context: str
    support_type: str
    relation_reason: str


class AuditClaim(BaseModel):
    claim_id: str
    claim_text: str
    risk_terms: list[RiskTerm]
    evidence: list[AuditEvidence]
    unverified_assumptions: list[str]


class SampleCheck(BaseModel):
    target_population: str
    sample_summary: str
    coverage_gaps: list[str]
    status: str


class AuditExtraction(BaseModel):
    claims: list[AuditClaim]
    sample_check: SampleCheck


class WorkflowField(BaseModel):
    name: str
    label: str
    kind: Literal["text", "select", "file"]
    required: bool = False
    help: str = ""
    accept: str = ""
    options: list[dict[str, str]] = Field(default_factory=list)
    show_when: dict[str, str] | None = None


class WorkflowSpec(BaseModel):
    id: str
    title: str
    description: str
    fields: list[WorkflowField]
