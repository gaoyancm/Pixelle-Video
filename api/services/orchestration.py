"""Application service for the phase 04-E LLM orchestration pipeline."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.orchestration.agents.decision_agent import DecisionAgent
from pixelle_video.orchestration.models import ContentPlan
from pixelle_video.orchestration.pipeline import OrchestrationPipeline
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.orchestration.router import IntentRouter


class OrchestrationService:
    """Own content-plan business rules and the L1-L4 orchestration flow."""

    def __init__(
        self,
        repository: ContentPlanRepository,
        *,
        intent_router: IntentRouter | None = None,
        decision_agent: DecisionAgent | None = None,
        pipeline: OrchestrationPipeline | None = None,
        llm_caller: Callable[[str], Awaitable[str]] | None = None,
        episode_planner: Any | None = None,
    ):
        self.repository = repository
        self.intent_router = intent_router or IntentRouter()
        self.decision_agent = decision_agent
        self.pipeline = pipeline
        self.llm_caller = llm_caller
        self.episode_planner = episode_planner

    # --- L1 ---------------------------------------------------------------------

    async def create_plan(self, body) -> Any:
        intent = body.intent or await self.intent_router.classify(body.request_text)
        if intent not in {"product_ad", "short_video", "animation", "unknown"}:
            intent = "unknown"
        plan_json = self.intent_router.build_initial_plan(body.request_text, intent)
        return await self.repository.create_plan(
            request_text=body.request_text,
            intent=intent,
            plan_json=plan_json,
            project_id=body.project_id,
        )

    async def get_plan(self, plan_id: str):
        return await self.repository.get_plan(plan_id)

    # --- L2/L3 ------------------------------------------------------------------

    async def generate(self, plan_id: str) -> dict[str, Any]:
        plan = await self._require(plan_id)
        if self.pipeline is None:
            raise RuntimeError("pipeline not configured")
        updated = await self.pipeline.run(plan)
        return {
            "plan_id": plan_id,
            "status": updated.status,
            "checkpoint": updated.checkpoint_json,
        }

    async def resume(self, plan_id: str) -> dict[str, Any]:
        return await self.generate(plan_id)

    async def status(self, plan_id: str) -> dict[str, Any]:
        plan = await self._require(plan_id)
        checkpoint = plan.checkpoint_json or {}
        return {
            "plan_id": plan_id,
            "status": plan.status,
            "current_stage": checkpoint.get("current_stage"),
            "completed_stages": checkpoint.get("completed_stages", []),
            "total_cost_so_far": checkpoint.get("total_cost_so_far", 0.0),
        }

    # --- L4 ---------------------------------------------------------------------

    async def approve(self, plan_id: str) -> dict[str, Any]:
        plan = await self.repository.update_plan(plan_id, status="approved")
        return {"plan_id": plan_id, "status": plan.status}

    async def reject(self, plan_id: str, reason: str) -> dict[str, Any]:
        plan = await self.repository.update_plan(plan_id, status="rejected")
        return {"plan_id": plan_id, "status": plan.status}

    async def retry_stage(self, plan_id: str, stage: str) -> dict[str, Any]:
        """Reset one stage (drop its result + completed marker) and re-run."""
        plan = await self._require(plan_id)
        checkpoint = dict(plan.checkpoint_json or {})
        completed = list(checkpoint.get("completed_stages", []))
        if stage in completed:
            completed.remove(stage)
        checkpoint["completed_stages"] = completed
        checkpoint["stage_results"].pop(stage, None)
        checkpoint["retry_count"][stage] = 0
        await self.repository.update_plan(plan_id, status="generating", checkpoint_json=checkpoint)
        return await self.generate(plan_id)

    async def generate_episode_plan(self, plan_id: str) -> dict[str, Any]:
        """D2: extend a Content Plan with season/episode structure, character
        arcs and foreshadowing (reuses plan_json — no new tables)."""
        plan = await self._require(plan_id)
        if plan.intent != "animation":
            raise ValueError(f"plan intent {plan.intent} is not animation")
        if self.episode_planner is None:
            raise RuntimeError("episode planner not configured")
        plan_json = dict(plan.plan_json or {})
        prompt = (
            f"[run_storyboard_planner] 为动画项目规划多集结构。世界观："
            f"{plan_json.get('summary', plan.request_text[:100])}。"
            "输出 seasons（season_no/episodes[episode_no/title/hook/arc]）、"
            "character_arcs（character_id/season_arc/key_episodes）、"
            "foreshadowing_map（setup/payoff）。"
        )
        result = await self.episode_planner.run(prompt)
        content = result.content
        plan_json["seasons"] = content.get("seasons", [])
        plan_json["character_arcs"] = content.get("character_arcs", [])
        plan_json["foreshadowing_map"] = content.get("foreshadowing_map", [])
        updated = await self.repository.update_plan(plan_id, plan_json=plan_json)
        return {
            "plan_id": plan_id,
            "seasons": updated.plan_json.get("seasons", []),
            "character_arcs": updated.plan_json.get("character_arcs", []),
            "foreshadowing_map": updated.plan_json.get("foreshadowing_map", []),
        }

    async def approval_summary(self, plan_id: str) -> dict[str, Any]:
        plan = await self._require(plan_id)
        checkpoint = plan.checkpoint_json or {}
        supervision = checkpoint.get("stage_results", {}).get("supervision", {})
        return {
            "plan_id": plan_id,
            "summary": plan.plan_json.get("summary", ""),
            "cost_estimate": plan.cost_estimate,
            "grade": supervision.get("grade"),
            "issues": [
                {"suggestion": suggestion} for suggestion in supervision.get("suggestions", [])
            ],
            "details": {
                "intent": plan.intent,
                "status": plan.status,
                "completed_stages": checkpoint.get("completed_stages", []),
            },
        }

    # --- helpers -----------------------------------------------------------------

    async def _require(self, plan_id: str) -> ContentPlan:
        plan = await self.repository.get_plan(plan_id)
        if plan is None:
            from pixelle_video.orchestration.repository import ContentPlanNotFoundError

            raise ContentPlanNotFoundError("content plan not found")
        return plan
