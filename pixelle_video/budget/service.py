"""Phase 03-F budget service: estimate, check, warn, cap, and usage queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.management.models import ProductionBatch, ProductionItem, ProductionItemAttempt
from pixelle_video.media_jobs.models import MediaJob
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus
from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS

from .models import BudgetConfig
from .repository import BudgetRepository

VALID_MODES = ("observe", "warn", "cap")


class BudgetBlockedError(RuntimeError):
    """Raised when cap mode blocks a new task or batch."""

    def __init__(self, message: str, *, limit: float | None = None, estimated: float | None = None):
        super().__init__(message)
        self.limit = limit
        self.estimated = estimated


class BudgetConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    mode: str
    estimated_cost: float | None
    limit: float | None
    warning: str | None = None


def default_cost_estimate(workflow_type: str) -> float | None:
    """Return a coarse unit cost estimate per workflow when no real pricing exists."""
    spec = WORKFLOW_SPECS.get(workflow_type)
    if spec is None:
        return None
    return 1.5 if spec.requires_image else 1.0


class BudgetService:
    """Enforce per-task and per-batch limits with observe / warn / cap modes."""

    def __init__(
        self,
        repository: BudgetRepository,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        estimate: Callable[[str], float | None] = default_cost_estimate,
        job_repository: MediaJobRepository | None = None,
    ):
        self.repository = repository
        self._session_factory = session_factory
        self.estimate = estimate
        self.job_repository = job_repository

    async def get_config(self) -> BudgetConfig:
        return await self.repository.get_config()

    async def update_config(
        self,
        *,
        per_task_limit: float | None,
        per_batch_limit: float | None,
        mode: str,
    ) -> BudgetConfig:
        if mode not in VALID_MODES:
            raise BudgetConfigurationError("budget mode must be observe, warn, or cap")
        if per_task_limit is not None and per_task_limit < 0:
            raise BudgetConfigurationError("per_task_limit must not be negative")
        if per_batch_limit is not None and per_batch_limit < 0:
            raise BudgetConfigurationError("per_batch_limit must not be negative")
        return await self.repository.update_config(
            per_task_limit=per_task_limit,
            per_batch_limit=per_batch_limit,
            mode=mode,
        )

    async def check_job_creation(self, workflow_type: str) -> BudgetDecision:
        """Decide whether a new job may be created under the current budget."""
        config = await self.get_config()
        estimated = self.estimate(workflow_type)
        limit = config.per_task_limit
        if limit is None or estimated is None or estimated <= limit:
            return BudgetDecision(
                allowed=True, mode=config.mode, estimated_cost=estimated, limit=limit
            )
        message = f"estimated cost {estimated:.2f} exceeds per-task limit {limit:.2f}"
        if config.mode == "cap":
            raise BudgetBlockedError(message, limit=limit, estimated=estimated)
        if config.mode == "warn":
            return BudgetDecision(
                allowed=True,
                mode=config.mode,
                estimated_cost=estimated,
                limit=limit,
                warning=message,
            )
        return BudgetDecision(allowed=True, mode=config.mode, estimated_cost=estimated, limit=limit)

    async def check_batch_submission(self, workflow_type: str, item_count: int) -> BudgetDecision:
        """Decide whether a batch submission may proceed under the per-batch limit."""
        config = await self.get_config()
        item_cost = self.estimate(workflow_type)
        estimated = None if item_cost is None else item_cost * item_count
        limit = config.per_batch_limit
        if limit is None or estimated is None or estimated <= limit:
            return BudgetDecision(
                allowed=True, mode=config.mode, estimated_cost=estimated, limit=limit
            )
        message = f"estimated batch cost {estimated:.2f} exceeds per-batch limit {limit:.2f}"
        if config.mode == "cap":
            raise BudgetBlockedError(message, limit=limit, estimated=estimated)
        if config.mode == "warn":
            return BudgetDecision(
                allowed=True,
                mode=config.mode,
                estimated_cost=estimated,
                limit=limit,
                warning=message,
            )
        return BudgetDecision(allowed=True, mode=config.mode, estimated_cost=estimated, limit=limit)

    async def record_job_cost(
        self,
        job_id: str,
        *,
        status: JobStatus,
        expected_version: int,
        estimated_cost: float | None = None,
        budget_warning: str | None = None,
    ) -> None:
        """Persist estimate and warning on a job without changing its status."""
        if self.job_repository is None:
            return
        if estimated_cost is None and budget_warning is None:
            return
        await self.job_repository.set_budget_fields(
            job_id,
            expected_status=status,
            expected_version=expected_version,
            estimated_cost=estimated_cost,
            budget_warning=budget_warning,
        )

    async def usage(self, project_id: str) -> dict[str, Any]:
        """Return aggregated cost usage for all jobs of one project."""
        statement = (
            select(
                func.count(MediaJob.job_id),
                func.coalesce(func.sum(MediaJob.estimated_cost), 0.0),
                func.coalesce(func.sum(MediaJob.actual_cost), 0.0),
            )
            .select_from(ProductionBatch)
            .join(ProductionItem, ProductionItem.batch_id == ProductionBatch.id)
            .join(ProductionItemAttempt, ProductionItemAttempt.item_id == ProductionItem.id)
            .join(MediaJob, MediaJob.job_id == ProductionItemAttempt.media_job_id)
            .where(ProductionBatch.project_id == project_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one()
        job_count, estimated_total, actual_total = row
        config = await self.get_config()
        return {
            "project_id": project_id,
            "job_count": int(job_count or 0),
            "estimated_total": float(estimated_total or 0.0),
            "actual_total": float(actual_total or 0.0),
            "per_task_limit": config.per_task_limit,
            "per_batch_limit": config.per_batch_limit,
            "mode": config.mode,
        }
