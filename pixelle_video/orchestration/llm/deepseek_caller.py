"""Phase 08 D2: real DeepSeek LLM caller (OpenAI-compatible chat API).

Replaces the deterministic mock for production use. Cost control: a hard
max_tokens cap keeps every call well under the ¥0.50 BudgetGuard budget
(DeepSeek pricing is ~¥0.001-0.002 per 1K tokens), and the 04-E audit
layer already records each llm_call with a cost snapshot.

Reasoning-model support: DeepSeek reasoning models (e.g. deepseek-v4-flash)
may return an empty ``content`` with the reasoning in ``reasoning_content``
on the first turn. Following the OpenAI-compatible convention, the caller
re-sends the assistant message (including reasoning) for up to 2 extra
turns until a non-empty final ``content`` arrives.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from pixelle_video.orchestration.contracts import (
    ConsistencyVerdictOutput,
    CopywriterOutput,
    EpisodePlanOutput,
    StoryboardOutput,
    StrategistOutput,
    SupervisionOutput,
)

DEFAULT_TIMEOUT = 60.0
MAX_TOKENS = 800  # hard cap: single call cost stays far below ¥0.50
MAX_TURNS = 3  # 1 initial + 2 follow-ups for reasoning models


def _json_type(prop: dict[str, Any]) -> str:
    """Human-readable type from a Pydantic JSON-schema property."""
    prop_type = prop.get("type")
    if prop_type == "array":
        items = prop.get("items", {})
        inner = _json_type(items) if isinstance(items, dict) and items else "any"
        return f"list[{inner}]"
    if prop_type == "object":
        return "dict"
    return str(prop_type or "any")


def _schema_instruction(prompt: str) -> str:
    """Append the expected output-schema field list for the requested tool.

    The 04-E sub-agents validate replies with Pydantic; the mock returned
    exact-shaped JSON, but a real LLM needs the schema spelled out. The
    field names and types come from the contracts' JSON schema so they
    never drift.
    """
    if "规划多集结构" in prompt:
        model = EpisodePlanOutput
    elif "run_content_strategist" in prompt:
        model = StrategistOutput
    elif "run_copywriter" in prompt:
        model = CopywriterOutput
    elif "run_consistency_verifier" in prompt:
        model = ConsistencyVerdictOutput
    elif "run_storyboard_planner" in prompt:
        model = StoryboardOutput
    elif "run_supervisor" in prompt:
        model = SupervisionOutput
    else:
        return ""
    schema = model.model_json_schema()
    parts = []
    for name, prop in schema.get("properties", {}).items():
        parts.append(f"{name}({_json_type(prop)})")
    fields = ", ".join(parts)
    extra = ""
    if model is StrategistOutput:
        extra = (
            " visual_style 必须是扁平 dict，其每个值都是字符串"
            '（如 {"palette": "蓝色", "mood": "未来感"}），值不要用数组；'
            "creative_directions 是对象数组，每项含 hook/angle 等字符串字段。"
        )
    return f"\n\n你必须只输出一个 JSON 对象，且必须包含以下字段（注意类型）：{fields}。{extra}"


async def deepseek_llm_caller(
    prompt: str,
    *,
    model: str,
    api_key: str,
    base_url: str,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """Call an OpenAI-compatible chat-completions endpoint and return the text."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a strict JSON API for a media-planning pipeline. "
                "Respond with ONLY a single valid JSON object matching the "
                "requested schema. No markdown fences, no explanations, no "
                "surrounding text."
            ),
        },
        {"role": "user", "content": prompt + _schema_instruction(prompt)},
    ]
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for _turn in range(MAX_TURNS):
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                }
                try:
                    response = await client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"deepseek call failed: {exc}") from exc
                if response.status_code != 200:
                    raise RuntimeError(
                        f"deepseek HTTP {response.status_code}: {response.text[:300]}"
                    )
                data = response.json()
                try:
                    message = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError(
                        f"unexpected deepseek response: {json.dumps(data)[:300]}"
                    ) from exc
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                # Reasoning model: keep the assistant turn for context and ask
                # for the final answer.
                assistant = {"role": "assistant", "content": message.get("content", "")}
                if message.get("reasoning_content"):
                    assistant["reasoning_content"] = message["reasoning_content"]
                messages.append(assistant)
                messages.append(
                    {
                        "role": "user",
                        "content": "请基于以上分析，直接给出最终答案。",
                    }
                )
    except RuntimeError:
        raise
    raise RuntimeError("deepseek returned empty content after all turns")
