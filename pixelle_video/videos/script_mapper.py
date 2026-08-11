"""Phase 06-F B1: map a 04-E Content Plan onto a video script.

The video_scripts table has no meta column; the plan trace is carried
inside the free-form script_json (plan_id field) — the 06 engines treat
script_json as opaque structured content.
"""

from __future__ import annotations

from typing import Any

PLAN_ID_KEY = "plan_id"


class ScriptMapper:
    """Field-level mapping from a Content Plan to a video script payload."""

    def map(self, plan) -> dict[str, Any]:
        plan_json = plan.plan_json or {}
        summary = plan_json.get("summary") or plan.request_text[:200]
        topic = self._topic(summary, plan.request_text)
        tasks = plan_json.get("tasks", [])
        scene_count = 3
        for task in tasks:
            count = task.get("count")
            if isinstance(count, int) and count > 0:
                scene_count = count
                break
        visual_style = plan_json.get("visual_style") or {}
        scenes = [
            {
                "index": index + 1,
                "visual_direction": str(
                    visual_style.get("mood") or visual_style.get("style") or "自然"
                ),
            }
            for index in range(scene_count)
        ]
        script_json: dict[str, Any] = {
            "hook": plan_json.get("target_audience") or summary[:60],
            "scenes": scenes,
            "visual_style": visual_style,
            "creative_directions": plan_json.get("creative_directions", []),
            PLAN_ID_KEY: plan.id,
        }
        return {
            "topic": topic,
            "script_json": script_json,
            "platform": self._platform(plan_json),
            "target_duration": 60,
            "language": "zh-CN",
            "plan_id": plan.id,
        }

    @staticmethod
    def _topic(summary: str, request_text: str) -> str:
        for source in (request_text, summary):
            for separator in ("，", ",", "；", "。", "！", "！", " "):
                head = source.split(separator)[0].strip()
                if head and len(head) <= 60:
                    return head
        return (summary or request_text)[:60]

    @staticmethod
    def _platform(plan_json: dict[str, Any]) -> str:
        platforms = plan_json.get("platforms") or []
        for name in ("tiktok", "douyin", "youtube", "shorts", "reels"):
            for platform in platforms:
                if name in str(platform).lower():
                    return "tiktok" if name == "shorts" else str(platform).lower()
        return "tiktok"

    @staticmethod
    def plan_id_from_script(script) -> str | None:
        script_json = script.script_json or {}
        return script_json.get(PLAN_ID_KEY)
