"""Experiment execution metrics for phase 04-C (E2).

All metrics are computed from existing persisted data; no new columns or
pipeline hooks are introduced:
- duration: media_jobs.finished_at - started_at
- cost: media_jobs.actual_cost (phase 03-F)
- qc score: latest audit qc_completed event (phase 04-B)
- size: output media asset size_bytes
- composite: weighted 0-100 score (cost 40 / qc 40 / time 20)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

from pixelle_video.experiments.models import ExperimentGroup
from pixelle_video.experiments.repository import ExperimentRepository


@dataclass(frozen=True)
class JobMetrics:
    job_id: str
    group_id: str
    group_name: str
    duration_seconds: float | None
    cost: float | None
    qc_score: float | None  # 0.0-1.0 or None when no QC result
    size_bytes: int | None
    composite: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "duration_seconds": self.duration_seconds,
            "cost": self.cost,
            "qc_score": self.qc_score,
            "size_bytes": self.size_bytes,
            "composite": self.composite,
        }


@dataclass
class GroupMetrics:
    group_id: str
    group_name: str
    prompt_version_id: str | None = None
    jobs: list[JobMetrics] = field(default_factory=list)

    @property
    def sample_size(self) -> int:
        return len(self.jobs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "prompt_version_id": self.prompt_version_id,
            "sample_size": self.sample_size,
            "jobs": [job.to_payload() for job in self.jobs],
        }


class ExperimentMetrics:
    """Compute per-job metrics and aggregate them per experiment group."""

    def __init__(
        self,
        repository: ExperimentRepository,
        *,
        job_lookup: Callable[[str], Awaitable[object]],
        qc_reader: Callable[[str], Awaitable[dict | None]],
        size_lookup: Callable[[str], Awaitable[int | None]],
    ):
        self.repository = repository
        self._job_lookup = job_lookup
        self._qc_reader = qc_reader
        self._size_lookup = size_lookup

    async def compute_job_metrics(
        self, job_id: str, group: ExperimentGroup, baseline: dict[str, float]
    ) -> JobMetrics:
        job = await self._job_lookup(job_id)
        duration = None
        cost = None
        if job is not None:
            started = getattr(job, "started_at", None)
            finished = getattr(job, "finished_at", None)
            if started is not None and finished is not None:
                duration = (finished - started).total_seconds()
            cost = getattr(job, "actual_cost", None)

        qc_result = await self._qc_reader(job_id)
        qc_score = None
        if qc_result is not None:
            total = qc_result.get("total_rules") or 0
            passed = qc_result.get("passed") or 0
            if total:
                qc_score = round(min(max(passed / total, 0.0), 1.0), 3)

        size = self._size_lookup(job_id)
        if hasattr(size, "__await__"):
            size = await size
        composite = self._composite(cost, qc_score, duration, baseline)
        return JobMetrics(
            job_id=job_id,
            group_id=group.id,
            group_name=group.group_name,
            duration_seconds=round(duration, 3) if duration is not None else None,
            cost=cost,
            qc_score=qc_score,
            size_bytes=size,
            composite=composite,
        )

    @staticmethod
    def _composite(
        cost: float | None,
        qc_score: float | None,
        duration: float | None,
        baseline: dict[str, float],
    ) -> float:
        max_cost = max(baseline.get("max_cost", 1.0), 1.0)
        min_cost = baseline.get("min_cost", 0.0)
        max_duration = max(baseline.get("max_duration", 0.0), 0.0)
        min_duration = baseline.get("min_duration", 0.0)

        if cost is None or max_cost <= min_cost:
            # Missing cost, or every job shares the same cost: treat as optimal.
            cost_component = 40.0
        else:
            cost_component = (1.0 - min(max(cost, 0.0) / max_cost, 1.0)) * 40.0

        qc_component = (qc_score or 0.0) * 40.0

        if duration is None or max_duration <= min_duration:
            time_component = 20.0
        else:
            normalized = (duration - min_duration) / (max_duration - min_duration)
            time_component = (1.0 - min(max(normalized, 0.0), 1.0)) * 20.0

        return round(cost_component + qc_component + time_component, 2)

    async def baseline_for_experiment(self, job_ids: Sequence[str]) -> dict[str, float]:
        costs: list[float] = []
        durations: list[float] = []
        for job_id in job_ids:
            job = await self._job_lookup(job_id)
            if job is None:
                continue
            cost = getattr(job, "actual_cost", None)
            if isinstance(cost, (int, float)):
                costs.append(float(cost))
            started = getattr(job, "started_at", None)
            finished = getattr(job, "finished_at", None)
            if started is not None and finished is not None:
                durations.append((finished - started).total_seconds())
        return {
            "max_cost": max(costs) if costs else 1.0,
            "min_cost": min(costs) if costs else 0.0,
            "max_duration": max(durations) if durations else 0.0,
            "min_duration": min(durations) if durations else 0.0,
        }

    async def compute_experiment(self, experiment_id: str) -> dict[str, Any]:
        groups = await self.repository.list_groups(experiment_id)
        jobs = await self.repository.list_jobs(experiment_id)
        baseline = await self.baseline_for_experiment([entry.job_id for entry in jobs])
        groups_by_id = {group.id: group for group in groups}
        collected: dict[str, GroupMetrics] = {}
        for group in groups:
            collected[group.id] = GroupMetrics(
                group.id, group.group_name, prompt_version_id=group.prompt_version_id
            )
        for entry in jobs:
            group = groups_by_id.get(entry.group_id)
            if group is None:
                continue
            collected[group.id].jobs.append(
                await self.compute_job_metrics(entry.job_id, group, baseline)
            )
        return {
            "experiment_id": experiment_id,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "groups": [metrics.to_payload() for metrics in collected.values()],
        }
