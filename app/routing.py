from __future__ import annotations

from app.evidence import clean_json_text
from app.llm import LLMClient
from app.models import IntentRouteResult
from app.routing_prompts import ROUTING_SYSTEM_PROMPT, build_routing_user_prompt

ROUTING_NODE_ID = "3L-0-1"


class IntentRouter:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def route(self, message: str) -> IntentRouteResult:
        content = await self.llm.complete(
            node_id=ROUTING_NODE_ID,
            system_prompt=ROUTING_SYSTEM_PROMPT,
            user_prompt=build_routing_user_prompt(message),
            temperature=0,
            json_model=IntentRouteResult,
        )
        return IntentRouteResult.model_validate_json(clean_json_text(content))
