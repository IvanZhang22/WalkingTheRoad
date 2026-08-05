from __future__ import annotations

import json
import re
from typing import Any


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
    data: dict[str, Any], source_text: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        raise EvidenceVerificationError("W3 证据提取结果缺少 evidence 数组。")

    rejected: list[dict[str, str]] = []
    for raw in evidence:
        if not isinstance(raw, dict):
            raise EvidenceVerificationError("W3 evidence 中存在非对象条目。")
        quote = str(raw.get("quote", "")).strip()
        status = _quote_status(quote, source_text)
        raw["verification"] = status
        if status == "rejected":
            rejected.append(
                {
                    "evidence_id": str(raw.get("evidence_id", "")),
                    "source_id": str(raw.get("source_id", "")),
                    "quote": quote,
                }
            )
            raw["quote"] = ""
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
