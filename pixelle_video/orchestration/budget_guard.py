"""Phase 04-E L4: budget guard (OpenMontage observe/warn/cap)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable


class BudgetExceededError(RuntimeError):
    pass


class BudgetGuard:
    """Cap LLM spend per plan using the 03-F budget config.

    The per-plan limit reuses the existing ``per_task_limit`` column
    (03-F table is not modified). The spend resolver is injected so the
    guard never touches existing repositories.
    """

    def __init__(
        self,
        config_reader: Callable[[], Awaitable[Any]],
        spent_resolver: Callable[[str], Awaitable[float]],
    ):
        self.config_reader = config_reader
        self.spent_resolver = spent_resolver

    async def check_can_spend(self, plan_id: str, amount: float) -> None:
        config = await self.config_reader()
        mode = getattr(config, "mode", None)
        if mode != "cap":
            return  # observe / warn never block
        limit = getattr(config, "per_task_limit", None)
        if limit is None:
            return
        spent = await self.spent_resolver(plan_id)
        if spent + amount > limit:
            raise BudgetExceededError(f"plan {plan_id} would exceed cap {limit} (spent {spent})")
