"""Phase 08 D2: real DeepSeek LLM caller (OpenAI-compatible chat API).

Replaces the deterministic mock for production use. Cost control: a hard
max_tokens cap keeps every call well under the ¥0.50 BudgetGuard budget
(DeepSeek pricing is ~¥0.001-0.002 per 1K tokens), and the 04-E audit
layer already records each llm_call with a cost snapshot.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_TIMEOUT = 60.0
MAX_TOKENS = 800  # hard cap: single call cost stays far below ¥0.50


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
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an expert creative media planner."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"deepseek call failed: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"deepseek HTTP {response.status_code}: {response.text[:300]}")
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected deepseek response: {json.dumps(data)[:300]}") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("deepseek returned empty content")
    return content
