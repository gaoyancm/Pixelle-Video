"""Phase 05-F A1: map a 04-E Content Plan onto a product brief.

The product_briefs table has no meta_json column and the contract forbids
new migrations, so the plan metadata (creative_directions + plan_id) is
carried in the brief's reference_images_json as a tagged entry; the 05
engines never read it as reference imagery.
"""

from __future__ import annotations

from typing import Any

PLAN_META_KEY = "plan_link"
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
        meta = {
            PLAN_ID_KEY: plan.id,
            CREATIVE_DIRECTIONS_KEY: plan_json.get("creative_directions", []),
        }
        return {
            "project_id": plan.project_id,
            "product_name": self._product_name(summary, plan.request_text),
            "description": f"{summary}\n[source_plan: {plan.id}]",
            "selling_points_json": existing_points,
            "target_audience": plan_json.get("target_audience"),
            "platforms_json": platforms,
            "reference_images_json": [meta],
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
        for entry in brief.reference_images_json or []:
            if isinstance(entry, dict) and entry.get(PLAN_ID_KEY):
                return entry
        return None
