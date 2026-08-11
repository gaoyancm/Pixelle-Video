"""Phase 04-E L2: the four Toonflow-style sub-agents.

Each sub-agent takes a natural-language prompt and returns a Pydantic
model. LLM execution goes through an injected mock callable (no real
LLM API); outputs are validated with Pydantic and retried up to
MAX_RETRIES with exponential backoff (ViMax pattern).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from pixelle_video.orchestration.contracts import (
    CopywriterOutput,
    StoryboardOutput,
    StrategistOutput,
    SubAgentResult,
    SupervisionOutput,
)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.05

LlmCaller = Callable[[str], Awaitable[str]]

# Pricing (USD per 1k tokens) used for mock cost accounting.
MODEL_PRICING = {"mock-llm": {"in": 0.001, "out": 0.002}}


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM reply."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in LLM reply")
    return json.loads(text[start : end + 1])


def _estimate_cost(tokens_in: int, tokens_out: int, model: str = "mock-llm") -> float:
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["mock-llm"])
    return round(tokens_in / 1000 * pricing["in"] + tokens_out / 1000 * pricing["out"], 6)


class SubAgent:
    """Base: compile prompt via 04-A, call LLM, validate with Pydantic + retry."""

    name = "sub_agent"

    def __init__(
        self,
        llm_caller: LlmCaller,
        *,
        prompt_compiler: Callable[..., str] | None = None,
    ):
        self.llm_caller = llm_caller
        self.prompt_compiler = prompt_compiler

    def _template(self) -> str:
        return "{{prompt}}"  # double braces: 04-A compile only matches {{var}}

    async def run(self, prompt: str) -> SubAgentResult:
        compiled = prompt
        if self.prompt_compiler is not None:
            compiled = self.prompt_compiler(self._template(), {"prompt": prompt})
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                reply = await self.llm_caller(compiled)
                tokens_in = max(len(compiled) // 4, 1)
                tokens_out = max(len(reply) // 4, 1)
                parsed = self._parse(reply)
                return SubAgentResult(
                    prompt=prompt,
                    content=parsed,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost=_estimate_cost(tokens_in, tokens_out),
                )
            except Exception as exc:  # noqa: BLE001 - Pydantic/JSON retry
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
        raise RuntimeError(f"{self.name} failed after {MAX_RETRIES} attempts: {last_error}")

    def _parse(self, reply: str) -> dict[str, Any]:
        raise NotImplementedError


class ContentStrategist(SubAgent):
    name = "content_strategist"

    def _parse(self, reply: str) -> dict[str, Any]:
        data = parse_json_object(reply)
        return StrategistOutput(**data).model_dump()


class Copywriter(SubAgent):
    name = "copywriter"

    def _parse(self, reply: str) -> dict[str, Any]:
        data = parse_json_object(reply)
        return CopywriterOutput(**data).model_dump()


class StoryboardPlanner(SubAgent):
    name = "storyboard_planner"

    def _parse(self, reply: str) -> dict[str, Any]:
        data = parse_json_object(reply)
        return StoryboardOutput(**data).model_dump()


class Supervisor(SubAgent):
    name = "supervisor"

    def _parse(self, reply: str) -> dict[str, Any]:
        data = parse_json_object(reply)
        return SupervisionOutput(**data).model_dump()

    @staticmethod
    def grade_from_counts(severe: int, medium: int) -> str:
        """Toonflow A/B/C/D rule, ported directly."""
        if severe == 0 and medium <= 2:
            return "A"
        if severe == 0 and medium <= 5:
            return "B"
        if 1 <= severe <= 2:
            return "C"
        return "D"


def grade_severity(grade: str) -> tuple[bool, str]:
    """Interpret a grade: (acceptable, reason)."""
    return grade in {"A", "B"}, f"grade {grade}"
