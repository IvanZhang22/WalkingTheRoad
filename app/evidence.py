from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from app.multimodal.models import MaterialSegment


class EvidenceVerificationError(ValueError):
    pass


def clean_json_text(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    return text


def load_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(clean_json_text(value))
    except json.JSONDecodeError as exc:
        raise EvidenceVerificationError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise EvidenceVerificationError("JSON 顶层必须是对象。")
    return parsed


def _quote_status(quote: str, source_text: str) -> str:
    if quote and quote in source_text:
        return "exact_match"
    if quote and re.sub(r"\s+", "", quote) in re.sub(r"\s+", "", source_text):
        return "whitespace_normalized_match"
    return "rejected"


def verify_material_evidence(
    data: dict[str, Any],
    source_text: str,
    allowed_segments: Mapping[str, MaterialSegment] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        raise EvidenceVerificationError("W3 证据提取结果缺少 evidence 数组。")

    rejected: list[dict[str, str]] = []
    for raw in evidence:
        if not isinstance(raw, dict):
            raise EvidenceVerificationError("W3 evidence 中存在非对象条目。")
        quote = str(raw.get("quote", "")).strip()
        segment = None
        if allowed_segments is not None:
            segment = allowed_segments.get(str(raw.get("source_id", "")))
            status = "exact_match" if segment is not None and quote in segment.text else "rejected"
        else:
            status = _quote_status(quote, source_text)
        raw["verification"] = status
        if status == "rejected":
            rejected_item = {
                "evidence_id": str(raw.get("evidence_id", "")),
                "source_id": str(raw.get("source_id", "")),
                "quote": quote,
            }
            if allowed_segments is not None:
                rejected_item["reason"] = "source_segment_not_found_or_quote_not_exact"
            rejected.append(rejected_item)
            raw["quote"] = ""
        elif segment is not None:
            raw["material_id"] = segment.material_id
            raw["source_segment_id"] = segment.segment_id
            raw["location"] = segment.locator.model_dump(mode="json", exclude_none=True)
            raw["provider_confidence"] = segment.provider_confidence
    data["verification_status"] = "verified"
    return data, rejected


def verify_audit_evidence(
    data: dict[str, Any], source_text: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise EvidenceVerificationError("W4 证据提取结果缺少 claims 数组。")

    rejected: list[dict[str, str]] = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("evidence"), list):
            raise EvidenceVerificationError("W4 claims/evidence 结构不符合约定。")
        for raw in claim["evidence"]:
            if not isinstance(raw, dict):
                raise EvidenceVerificationError("W4 evidence 中存在非对象条目。")
            quote = str(raw.get("quote", "")).strip()
            status = _quote_status(quote, source_text)
            raw["verification"] = status
            if status == "rejected":
                rejected.append(
                    {
                        "claim_id": str(claim.get("claim_id", "")),
                        "evidence_id": str(raw.get("evidence_id", "")),
                        "source_id": str(raw.get("source_id", "")),
                        "quote": quote,
                    }
                )
                raw["quote"] = ""
    data["status"] = "verified"
    return data, rejected
