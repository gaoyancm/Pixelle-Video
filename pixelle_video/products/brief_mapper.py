"""Phase 05-F A1: map a 04-E Content Plan onto a product brief.

The plan linkage is carried by the brief's dedicated ``plan_id`` column
(phase 10). ``reference_images_json`` is reserved for real reference imagery
and is never used to stash plan metadata.
"""

from __future__ import annotations

from typing import Any

PLAN_ID_KEY = "plan_id"
CREATIVE_DIRECTIONS_KEY = "creative_directions"


class BriefMapper:
    """Field-level mapping from plan_json to a product brief payload."""

    def map(self, plan) -> dict[str, Any]:
        plan_json = plan.plan_json or {}
        summary = plan_json.get("summary") or plan.request_text[:200]
        selling_points = [plan_json.get("visual_style", {}).get("palette")]
        selling_points = [point for point in selling_points if point]
        existing_points = list(selling_points)
        platforms = plan_json.get("platforms", [])
        if not platforms and plan.intent == "product_ad":
            platforms = ["etsy", "tiktok"]
        return {
            "project_id": plan.project_id,
            "product_name": self._product_name(summary, plan.request_text),
            "description": f"{summary}\n[source_plan: {plan.id}]",
            "selling_points_json": existing_points,
            "target_audience": plan_json.get("target_audience"),
            "platforms_json": platforms,
            "reference_images_json": [],
            "plan_id": plan.id,
        }

    @staticmethod
    def _product_name(summary: str, request_text: str) -> str:
        # Best-effort product name: first clause of the summary/request.
        for source in (request_text, summary):
            for separator in ("，", ",", "；", "。", " "):
                head = source.split(separator)[0].strip()
                if head and len(head) <= 40:
                    return head
        return (summary or request_text)[:40]

    @staticmethod
    def plan_link_from_brief(brief) -> dict[str, Any] | None:
        if getattr(brief, "plan_id", None):
            return {PLAN_ID_KEY: brief.plan_id}
        return None
