"""Phase 04-E L1: intent routing for natural-language requests."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

PRODUCT_KEYWORDS = (
    "主图",
    "广告图",
    "产品",
    "商品",
    "etspy",
    "etsy",
    "tiktok 广告",
    "带货",
    "商品详情",
    "主视觉",
    "电商图",
    "banner",
)
VIDEO_KEYWORDS = (
    "短视频",
    "视频",
    "选题",
    "口播",
    "vlog",
    "shorts",
    "reels",
    "脚本",
    "分镜",
    "解说",
    "科普",
)
ANIMATION_KEYWORDS = (
    "动画",
    "剧集",
    "长篇",
    "角色",
    "故事",
    "动漫",
    "番剧",
    "连续剧",
    "世界观",
)


class IntentRouter:
    """Keyword + heuristic intent classification with knowledge boost."""

    def __init__(
        self,
        *,
        knowledge_querier: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    ):
        self.knowledge_querier = knowledge_querier

    async def classify(self, text: str) -> str:
        lowered = text.lower()
        if any(word in text for word in PRODUCT_KEYWORDS) or any(
            word in lowered for word in ("etsy", "tiktok")
        ):
            if self.knowledge_querier is not None:
                await self.knowledge_querier(text)  # enrich decision via 04-D
            return "product_ad"
        if any(word in text for word in ANIMATION_KEYWORDS):
            return "animation"
        if any(word in text for word in VIDEO_KEYWORDS) or re.search(r"\b(video|short)\b", lowered):
            return "short_video"
        return "unknown"

    def build_initial_plan(self, request_text: str, intent: str) -> dict[str, Any]:
        """Seed a plan_json skeleton for the matched intent."""
        return {
            "intent": intent,
            "summary": request_text[:200],
            "target_audience": None,
            "platforms": [],
            "creative_directions": [],
            "visual_style": {},
            "tasks": [],
        }
