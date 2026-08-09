"""Application service for the phase 04-C experiment system."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.audit import AuditRepository
from pixelle_video.experiments.failure_feedback import FailureFeedbackEngine
from pixelle_video.experiments.metrics import ExperimentMetrics
from pixelle_video.experiments.repository import ExperimentRepository
from pixelle_video.experiments.stats import ExperimentStats


class ExperimentApplicationService:
    """Own experiment business rules and metric aggregation."""

    def __init__(
        self,
        repository: ExperimentRepository,
        *,
        job_lookup: Callable[[str], Awaitable[object]],
        audit: AuditRepository | None = None,
        size_lookup: Callable[[str], Awaitable[int | None]] | None = None,
        stats: ExperimentStats | None = None,
    ):
        self.repository = repository
        self.audit = audit
        self.stats = stats or ExperimentStats()
        self.metrics_engine = ExperimentMetrics(
            repository,
            job_lookup=job_lookup,
            qc_reader=self._read_qc_summary,
            size_lookup=size_lookup or (lambda job_id: _no_size(job_id)),
        )
        self.failure_engine = FailureFeedbackEngine(
            repository, qc_issue_reader=self._read_qc_issues
        )

    # --- E1 ---------------------------------------------------------------------

    async def create(self, body) -> Any:
        groups = [entry.model_dump() for entry in body.groups]
        return await self.repository.create_experiment(
            name=body.name,
            description=body.description,
            metric=body.metric,
            groups=groups,
        )

    async def list(self, status: str | None, limit: int, offset: int):
        return await self.repository.list_experiments(status=status, limit=limit, offset=offset)

    async def get(self, experiment_id: str):
        return await self.repository.get_experiment(experiment_id)

    async def update_status(self, experiment_id: str, status: str):
        return await self.repository.update_status(experiment_id, status)

    async def groups(self, experiment_id: str):
        return await self.repository.list_groups(experiment_id)

    async def add_job(self, job_id: str, experiment_id: str, group_id: str):
        await self.repository.add_job(job_id, experiment_id, group_id)

    # --- E2 ---------------------------------------------------------------------

    async def metrics(self, experiment_id: str) -> dict[str, Any]:
        return await self.metrics_engine.compute_experiment(experiment_id)

    async def jobs(self, experiment_id: str) -> list[dict[str, Any]]:
        entries = await self.repository.list_jobs(experiment_id)
        groups = {group.id: group for group in await self.repository.list_groups(experiment_id)}
        return [
            {
                "job_id": entry.job_id,
                "group_id": entry.group_id,
                "group_name": groups[entry.group_id].group_name if entry.group_id in groups else "",
                "created_at": entry.created_at,
            }
            for entry in entries
        ]

    # --- E3 ---------------------------------------------------------------------

    async def report(self, experiment_id: str) -> dict[str, Any]:
        experiment = await self.repository.get_experiment(experiment_id)
        if experiment is None:
            from pixelle_video.experiments.repository import ExperimentNotFoundError

            raise ExperimentNotFoundError("experiment not found")
        payload = await self.metrics_engine.compute_experiment(experiment_id)
        payload["metric"] = experiment.metric
        return self.stats.compare(payload)

    # --- E4 ---------------------------------------------------------------------

    async def collect_failures(self, experiment_id: str) -> list[Any]:
        experiment = await self.repository.get_experiment(experiment_id)
        if experiment is None:
            from pixelle_video.experiments.repository import ExperimentNotFoundError

            raise ExperimentNotFoundError("experiment not found")
        payload = await self.metrics_engine.compute_experiment(experiment_id)
        return await self.failure_engine.collect_failures(experiment_id, payload)

    async def failures(self, experiment_id: str) -> list[Any]:
        return await self.repository.list_failures_for_experiment(experiment_id)

    async def prompt_failures(self, prompt_version_id: str) -> list[Any]:
        return await self.repository.list_failures_for_prompt(prompt_version_id)

    # --- data readers -----------------------------------------------------------

    async def _read_qc_summary(self, job_id: str) -> dict | None:
        if self.audit is None:
            return None
        events, _ = await self.audit.list(
            scope_type="job", scope_id=job_id, event_type="qc_completed"
        )
        if not events:
            return None
        details = dict(events[-1].details_json or {})
        return {"total_rules": details.get("total_rules"), "passed": details.get("passed")}

    async def _read_qc_issues(self, job_id: str) -> list[dict]:
        if self.audit is None:
            return []
        events, _ = await self.audit.list(
            scope_type="job", scope_id=job_id, event_type="qc_completed"
        )
        if not events:
            return []
        details = dict(events[-1].details_json or {})
        return details.get("issues") or []


async def _no_size(job_id: str) -> int | None:
    del job_id
    return None
