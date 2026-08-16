"""把自动可用多模态片段准备为 W3 输入，并保留确定性定位索引。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from app.multimodal.models import Material, MaterialModality, MaterialSegment

MaterialSourceType = Literal["单份访谈", "多份访谈", "田野或观察笔记", "混合材料"]


@dataclass(frozen=True, slots=True)
class PreparedMaterialBundle:
    source_id: str
    source_type: MaterialSourceType
    source_context: str
    display_name: str
    source_text: str
    character_count: int
    sha256: str
    segment_index: dict[str, MaterialSegment]


def prepare_w3_material_bundle(
    materials: list[Material], *, max_characters: int
) -> PreparedMaterialBundle:
    """只序列化 `automatic_evidence_use=true` 的片段，复核队列永不进入提示词。"""

    segment_index: dict[str, MaterialSegment] = {}
    segment_rows: list[dict[str, object]] = []
    included_materials: list[Material] = []
    character_count = 0
    for material in materials:
        included = False
        for segment in material.segments:
            if not segment.automatic_evidence_use or segment.segment_id in segment_index:
                continue
            character_count += len(segment.text)
            if character_count > max_characters:
                raise ValueError(
                    f"自动可用材料正文合计超过 {max_characters} 字，请拆分后分批分析。"
                )
            segment_index[segment.segment_id] = segment
            segment_rows.append(
                {
                    "source_segment_id": segment.segment_id,
                    "material_id": segment.material_id,
                    "modality": segment.modality.value,
                    "text": segment.text,
                    "location": segment.locator.model_dump(mode="json", exclude_none=True),
                    "provider_confidence": segment.provider_confidence,
                }
            )
            included = True
        if included:
            included_materials.append(material)
    if not segment_rows:
        raise ValueError("没有通过自动证据门控的材料片段，不能执行 W3。")

    fingerprint = hashlib.sha256(
        "\0".join(material.source_fingerprint for material in included_materials).encode("utf-8")
    ).hexdigest()
    names = [material.filename for material in included_materials]
    return PreparedMaterialBundle(
        source_id=f"PACK_MM_{fingerprint[:10].upper()}",
        source_type=_source_type(included_materials),
        source_context=f"由统一多模态入口自动接入：{'、'.join(names)}"[:4000],
        display_name="、".join(names)[:300],
        source_text=json.dumps({"material_segments": segment_rows}, ensure_ascii=False, indent=2),
        character_count=character_count,
        sha256=fingerprint,
        segment_index=segment_index,
    )


def _source_type(materials: list[Material]) -> MaterialSourceType:
    modalities = {material.modality for material in materials}
    if modalities == {MaterialModality.audio}:
        return "单份访谈" if len(materials) == 1 else "多份访谈"
    if modalities == {MaterialModality.image}:
        return "田野或观察笔记"
    return "混合材料"
