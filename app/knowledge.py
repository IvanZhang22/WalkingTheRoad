"""Small, review-aware methodology knowledge base for 行小道.

This module intentionally indexes curated rule cards rather than source books.
Candidate cards can guide a response but never become user-material evidence or
an asserted, already-reviewed academic rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", re.IGNORECASE)
_FORMAL_STATUSES = {"approved_rule", "reviewed"}
_ADVISORY_STATUSES = {"candidate_v2"}


@dataclass(frozen=True)
class KnowledgeCard:
    card_id: str
    workflow: str
    title: str
    instruction: str
    prohibition: str
    review_status: str
    version: str
    source: str
    applicability: str
    boundary: str
    kind: str = "rule"

    @property
    def advisory(self) -> bool:
        return self.review_status in _ADVISORY_STATUSES


@dataclass(frozen=True)
class KnowledgeHit:
    card: KnowledgeCard
    score: int


class MethodologyKnowledgeBase:
    """File-backed, deterministic retrieval with no external database dependency."""

    def __init__(self, cards: list[KnowledgeCard], version: str = "2.1.0") -> None:
        self.cards = cards
        self.version = version

    @classmethod
    def from_directory(cls, directory: Path) -> MethodologyKnowledgeBase:
        manifest_path = directory / "manifest.json"
        rules_path = directory / "rules_v2.1.0.jsonl"
        cases_path = directory / "case_cards_v2.1.0.jsonl"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cards: list[KnowledgeCard] = []
        for path, kind in ((rules_path, "rule"), (cases_path, "case")):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                cards.append(
                    KnowledgeCard(
                        card_id=str(raw["card_id"]),
                        workflow=str(raw["workflow"]).lower(),
                        title=str(raw["title"]),
                        instruction=str(raw.get("instruction", raw.get("summary", ""))),
                        prohibition=str(raw.get("prohibition", "")),
                        review_status=str(raw.get("review_status", "candidate_v2")),
                        version=str(raw.get("version", manifest["knowledge_version"])),
                        source=str(
                            raw.get("source")
                            or "、".join(str(item) for item in raw.get("sources", ()))
                            or raw.get("source_file", "资料库候选卡")
                        ),
                        applicability=str(raw.get("applicability", "与当前工作流和任务相符时")),
                        boundary=str(raw.get("boundary", "仅作提示，需研究者结合情境判断")),
                        kind=kind,
                    )
                )
        return cls(cards=cards, version=str(manifest["knowledge_version"]))

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = text.lower()
        tokens: set[str] = set(_TOKEN_RE.findall(normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
        return tokens

    def retrieve(self, query: str, workflow: str | None, limit: int = 4) -> list[KnowledgeHit]:
        query_tokens = self._tokens(query)
        scoped = (workflow or "").lower()
        candidates = [
            card
            for card in self.cards
            if card.review_status in _FORMAL_STATUSES | _ADVISORY_STATUSES
            and (not scoped or card.workflow in {"global", scoped})
        ]
        hits: list[KnowledgeHit] = []
        for card in candidates:
            content_tokens = self._tokens(
                " ".join((card.title, card.instruction, card.prohibition, card.applicability, card.boundary))
            )
            overlap = len(query_tokens & content_tokens)
            workflow_bonus = 5 if scoped and card.workflow == scoped else 2 if card.workflow == "global" else 0
            # Examples never displace actual method rules; they are optional, low-priority context.
            kind_penalty = 0 if card.kind == "rule" else 100
            hits.append(KnowledgeHit(card=card, score=overlap * 10 + workflow_bonus - kind_penalty))
        hits.sort(key=lambda item: (-item.score, item.card.kind != "rule", item.card.card_id))
        return hits[:limit]

    def prompt_context(self, query: str, workflow: str | None) -> str:
        hits = self.retrieve(query, workflow)
        if not hits:
            return "未检索到适用的方法依据；不得把常识写成资料库支持。"
        lines = [
            "以下是内部方法参考。它们不是用户材料，也不能当作用户项目的事实或引文。",
            "候选卡仅作建议；不得称为已审核规范、不得替代研究者判断。",
        ]
        for hit in hits:
            card = hit.card
            lines.append(
                f"[{card.card_id}|{card.title}|{card.review_status}|{card.version}] "
                f"建议：{card.instruction} 边界：{card.boundary}"
            )
            if card.prohibition:
                lines.append(f"禁止：{card.prohibition}")
        return "\n".join(lines)

    def source_markdown(self, query: str, workflow: str | None) -> str:
        hits = self.retrieve(query, workflow)
        if not hits:
            return "## 一、方法依据\n\n本次未检索到适用的方法依据；以上建议仅来自当前对话，不应视为资料库结论。"
        lines = ["## 一、方法依据", "", "以下均为候选方法依据，尚待专家复核，不是强制规范。", ""]
        for index, hit in enumerate(hits, start=1):
            card = hit.card
            lines.extend(
                [
                    f"### {index}、{card.title}",
                    "",
                    f"`[{card.card_id}｜{card.source}｜{card.review_status}｜v{card.version}]`",
                    "",
                    f"- 建议：{card.instruction}",
                    f"- 适用边界：{card.boundary}",
                ]
            )
        return "\n".join(lines)

    def status(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for card in self.cards:
            counts[card.review_status] = counts.get(card.review_status, 0) + 1
        return {
            "enabled": True,
            "knowledge_version": self.version,
            "card_count": len(self.cards),
            "review_status_counts": counts,
            "formal_rule_count": sum(card.review_status in _FORMAL_STATUSES for card in self.cards),
            "advisory_only": True,
        }
