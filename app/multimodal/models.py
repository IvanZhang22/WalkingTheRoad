"""与具体 ASR/OCR 厂商无关的统一材料模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MaterialModality(StrEnum):
    audio = "audio"
    image = "image"
    document = "document"


class MaterialStatus(StrEnum):
    ready = "ready"
    manual_review = "manual_review"
    failed = "failed"


class MaterialLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None

    @field_validator("bbox")
    @classmethod
    def non_negative_bbox(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if value is not None and any(number < 0 for number in value):
            raise ValueError("bbox 坐标不得为负数")
        return value

    @model_validator(mode="after")
    def ordered_ranges(self) -> MaterialLocator:
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end < self.char_start
        ):
            raise ValueError("char_end 不得小于 char_start")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms 不得小于 start_ms")
        return self


class ProviderSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=100_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    locator: MaterialLocator
    speaker: str | None = Field(default=None, max_length=100)


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_name: str = Field(min_length=1, max_length=100)
    provider_model: str = Field(min_length=1, max_length=200)
    segments: list[ProviderSegment] = Field(default_factory=list, max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class MaterialSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    segment_id: str = Field(min_length=1, max_length=100)
    material_id: str = Field(min_length=1, max_length=100)
    modality: MaterialModality
    text: str = Field(min_length=1, max_length=100_000)
    provider_confidence: float | None = Field(default=None, ge=0, le=1)
    locator: MaterialLocator
    automatic_evidence_use: bool = False
    quality_flags: list[str] = Field(default_factory=list, max_length=20)
    speaker: str | None = Field(default=None, max_length=100)


class MaterialIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False


class Material(BaseModel):
    """可交给业务层的材料；不保存或返回原始附件 URL。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    material_id: str = Field(min_length=1, max_length=100)
    source_fingerprint: str = Field(min_length=16, max_length=64)
    filename: str = Field(min_length=1, max_length=300)
    modality: MaterialModality
    status: MaterialStatus
    normalized_text: str = Field(default="", max_length=300_000)
    automatic_text: str = Field(default="", max_length=300_000)
    provider_name: str = Field(default="", max_length=100)
    provider_model: str = Field(default="", max_length=200)
    segments: list[MaterialSegment] = Field(default_factory=list, max_length=10_000)
    automatic_evidence_use: bool = False
    review_queue: list[str] = Field(default_factory=list, max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    issues: list[MaterialIssue] = Field(default_factory=list, max_length=100)
