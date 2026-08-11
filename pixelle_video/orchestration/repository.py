"""Short-transaction repository for the phase 04-E content plans."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import ContentPlan


class ContentPlanNotFoundError(RuntimeError):
    pass


class ContentPlanRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_plan(
        self,
        *,
        request_text: str,
        intent: str,
        plan_json: dict[str, Any],
        project_id: str | None = None,
        status: str = "draft",
        cost_estimate: float | None = None,
        checkpoint_json: dict[str, Any] | None = None,
        plan_id: str | None = None,
    ) -> ContentPlan:
        plan = ContentPlan(
            id=plan_id or str(uuid.uuid4()),
            project_id=project_id,
            request_text=request_text,
            intent=intent,
            plan_json=plan_json,
            status=status,
            cost_estimate=cost_estimate,
            checkpoint_json=checkpoint_json,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(plan)
                await session.flush()
                return plan

    async def get_plan(self, plan_id: str) -> ContentPlan | None:
        async with self._session_factory() as session:
            return await session.get(ContentPlan, plan_id)

    async def update_plan(
        self,
        plan_id: str,
        *,
        plan_json: dict[str, Any] | None = None,
        status: str | None = None,
        cost_estimate: float | None = None,
        checkpoint_json: dict[str, Any] | None = None,
    ) -> ContentPlan:
        async with self._session_factory() as session:
            async with session.begin():
                plan = await session.get(ContentPlan, plan_id)
                if plan is None:
                    raise ContentPlanNotFoundError("content plan not found")
                if plan_json is not None:
                    plan.plan_json = plan_json
                if status is not None:
                    plan.status = status
                if cost_estimate is not None:
                    plan.cost_estimate = cost_estimate
                if checkpoint_json is not None:
                    plan.checkpoint_json = checkpoint_json
                await session.flush()
                return plan

    async def list_plans(
        self, *, project_id: str | None = None, limit: int, offset: int
    ) -> tuple[list[ContentPlan], bool]:
        statement = select(ContentPlan).order_by(
            ContentPlan.created_at.desc(), ContentPlan.id.desc()
        )
        if project_id is not None:
            statement = statement.where(ContentPlan.project_id == project_id)
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit
