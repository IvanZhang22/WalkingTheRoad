import json

import pytest

from app.evidence import (
    EvidenceVerificationError,
    clean_json_text,
    load_json,
    verify_audit_evidence,
    verify_material_evidence,
)


def test_clean_json_fence_and_load() -> None:
    raw = '```json\n{"value": 1}\n```'
    assert clean_json_text(raw) == '{"value": 1}'
    assert load_json(raw) == {"value": 1}


def test_load_json_rejects_non_object() -> None:
    with pytest.raises(EvidenceVerificationError, match="顶层"):
        load_json("[]")


def test_w3_exact_whitespace_and_rejected_quotes() -> None:
    source = "I01\n原文 连续片段。\nI02\n另一个回答。"
    data = {
        "evidence": [
            {"evidence_id": "E1", "source_id": "I01", "quote": "原文 连续片段。"},
            {"evidence_id": "E2", "source_id": "I01", "quote": "原文连续片段。"},
            {"evidence_id": "E3", "source_id": "I99", "quote": "模型编造内容"},
        ]
    }
    verified, rejected = verify_material_evidence(data, source)
    assert verified["evidence"][0]["verification"] == "exact_match"
    assert verified["evidence"][1]["verification"] == "whitespace_normalized_match"
    assert verified["evidence"][2]["verification"] == "rejected"
    assert verified["evidence"][2]["quote"] == ""
    assert rejected == [{"evidence_id": "E3", "source_id": "I99", "quote": "模型编造内容"}]


def test_w4_rejected_quote_keeps_claim_reference() -> None:
    data = {
        "claims": [
            {
                "claim_id": "C02",
                "evidence": [{"evidence_id": "E7", "source_id": "I01", "quote": "不存在的原话"}],
            }
        ]
    }
    verified, rejected = verify_audit_evidence(data, "真实材料")
    assert verified["status"] == "verified"
    assert rejected[0]["claim_id"] == "C02"
    assert json.dumps(rejected, ensure_ascii=False).find("不存在的原话") > 0
