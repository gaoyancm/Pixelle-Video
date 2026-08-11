"""Phase 04-E L3: pipeline with ViMax-style checkpoints."""

from __future__ import annotations

import json
from typing import Any, Callable

from pixelle_video.orchestration.agents.decision_agent import DecisionAgent
from pixelle_video.orchestration.agents.sub_agents import MAX_RETRIES, Supervisor
from pixelle_video.orchestration.models import ContentPlan
from pixelle_video.orchestration.repository import ContentPlanRepository

STAGE_ORDER = (
    "intent_classification",
    "audience_analysis",
    "copy_writing",
    "storyboard",
    "supervision",
)

STAGE_TOOL = {
    "intent_classification": "run_content_strategist",
    "audience_analysis": "run_content_strategist",
    "copy_writing": "run_copywriter",
    "storyboard": "run_storyboard_planner",
    "supervision": "run_supervisor",
}


class PipelineError(RuntimeError):
    pass


class OrchestrationPipeline:
    """Run the stage machine with JSON checkpoints for resumable execution."""

    def __init__(
        self,
        repository: ContentPlanRepository,
        decision_agent: DecisionAgent,
        *,
        supervisor: Supervisor | None = None,
        budget_guard: Any | None = None,
        audit_recorder: Callable[..., Any] | None = None,
    ):
        self.repository = repository
        self.decision_agent = decision_agent
        self.supervisor = supervisor
        self.budget_guard = budget_guard
        self.audit_recorder = audit_recorder

    # --- checkpoint helpers -------------------------------------------------------

    @staticmethod
    def fresh_checkpoint() -> dict[str, Any]:
        return {
            "current_stage": None,
            "completed_stages": [],
            "stage_results": {},
            "retry_count": {},
            "total_cost_so_far": 0.0,
        }

    def _merge_checkpoint(self, plan: ContentPlan) -> dict[str, Any]:
        checkpoint = dict(plan.checkpoint_json or self.fresh_checkpoint())
        checkpoint.setdefault("completed_stages", [])
        checkpoint.setdefault("stage_results", {})
        checkpoint.setdefault("retry_count", {})
        checkpoint.setdefault("total_cost_so_far", 0.0)
        return checkpoint

    # --- execution ---------------------------------------------------------------

    async def run(self, plan: ContentPlan) -> ContentPlan:
        checkpoint = self._merge_checkpoint(plan)
        await self.repository.update_plan(plan.id, status="generating")

        for stage in STAGE_ORDER:
            if stage in checkpoint["completed_stages"]:
                continue  # resume skips finished stages
            checkpoint["current_stage"] = stage
            result = await self._run_stage(plan, stage, checkpoint)
            checkpoint["stage_results"][stage] = result
            checkpoint["completed_stages"].append(stage)
            checkpoint["retry_count"][stage] = 0
            await self.repository.update_plan(
                plan.id,
                checkpoint_json=checkpoint,
                cost_estimate=checkpoint["total_cost_so_far"],
            )

        checkpoint["current_stage"] = None
        final_status = "awaiting_approval"
        await self.repository.update_plan(plan.id, status=final_status, checkpoint_json=checkpoint)
        return await self.repository.get_plan(plan.id)

    async def _run_stage(
        self, plan: ContentPlan, stage: str, checkpoint: dict[str, Any]
    ) -> dict[str, Any]:
        """One stage: dispatch to its sub-agent, budget-check, retry on failure."""
        tool = STAGE_TOOL[stage]
        attempts = checkpoint["retry_count"].get(stage, 0)
        while attempts < MAX_RETRIES:
            if self.budget_guard is not None and stage in {
                "copy_writing",
                "storyboard",
            }:
                await self.budget_guard.check_can_spend(plan.id, 0.01)
            try:
                dispatched = await self.decision_agent.execute_tool(tool, plan.request_text)
                sub_result = dispatched["result"]
                content: dict[str, Any] = sub_result.content
                cost = float(sub_result.cost)
                checkpoint["total_cost_so_far"] = round(checkpoint["total_cost_so_far"] + cost, 6)
                if self.audit_recorder is not None:
                    await self.audit_recorder(
                        event_type="llm_call",
                        scope_type="content_plan",
                        scope_id=plan.id,
                        details={
                            "stage": stage,
                            "tool": tool,
                            "tokens_in": sub_result.tokens_in,
                            "tokens_out": sub_result.tokens_out,
                        },
                        cost_snapshot={"cost": cost},
                    )
                return content
            except Exception as exc:  # noqa: BLE001 - stage retry
                attempts += 1
                checkpoint["retry_count"][stage] = attempts
                await self.repository.update_plan(plan.id, checkpoint_json=checkpoint)
                if attempts >= MAX_RETRIES:
                    await self.repository.update_plan(plan.id, status="stage_failed")
                    raise PipelineError(
                        f"stage {stage} failed after {MAX_RETRIES} attempts: {exc}"
                    ) from exc
        raise PipelineError(f"stage {stage} exhausted retries")

    # --- supervision -------------------------------------------------------------

    async def run_supervision(self, plan: ContentPlan, result: dict[str, Any]) -> dict[str, Any]:
        if self.supervisor is None:
            raise RuntimeError("supervisor not configured")
        supervised = await self.supervisor.run(plan.request_text)
        content = supervised["content"]
        return {
            "grade": content.get("grade"),
            "severe_issues": content.get("severe_issues", 0),
            "medium_issues": content.get("medium_issues", 0),
            "suggestions": content.get("suggestions", []),
        }


def serialize_checkpoint(checkpoint: dict[str, Any]) -> str:
    return json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
