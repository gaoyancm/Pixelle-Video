"""Short-transaction repository for the phase 04-C experiment system."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Experiment, ExperimentGroup, ExperimentJob, FailureSample


class ExperimentNotFoundError(RuntimeError):
    pass


class ExperimentGroupNotFoundError(RuntimeError):
    pass


class ExperimentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_experiment(
        self,
        *,
        name: str,
        description: str | None = None,
        metric: str = "composite",
        groups: Sequence[dict[str, Any]] | None = None,
        experiment_id: str | None = None,
    ) -> Experiment:
        experiment = Experiment(
            id=experiment_id or str(uuid.uuid4()),
            name=name,
            description=description,
            metric=metric,
        )
        group_entries = list(groups or [])
        async with self._session_factory() as session:
            async with session.begin():
                session.add(experiment)
                await session.flush()
                for entry in group_entries:
                    session.add(
                        ExperimentGroup(
                            id=str(uuid.uuid4()),
                            experiment_id=experiment.id,
                            group_name=str(entry.get("group_name") or "对照组"),
                            prompt_version_id=entry.get("prompt_version_id"),
                            model_name=entry.get("model_name"),
                            params_json=entry.get("params_json"),
                        )
                    )
                await session.flush()
        return experiment

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        async with self._session_factory() as session:
            return await session.get(Experiment, experiment_id)

    async def list_experiments(
        self, *, status: str | None = None, limit: int, offset: int
    ) -> tuple[list[Experiment], bool]:
        statement = select(Experiment).order_by(Experiment.created_at.desc(), Experiment.id.desc())
        if status is not None:
            statement = statement.where(Experiment.status == status)
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit

    async def update_status(self, experiment_id: str, status: str) -> Experiment:
        async with self._session_factory() as session:
            async with session.begin():
                experiment = await session.get(Experiment, experiment_id)
                if experiment is None:
                    raise ExperimentNotFoundError("experiment not found")
                experiment.status = status
                await session.flush()
                return experiment

    async def list_groups(self, experiment_id: str) -> list[ExperimentGroup]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentGroup)
                .where(ExperimentGroup.experiment_id == experiment_id)
                .order_by(ExperimentGroup.created_at.asc(), ExperimentGroup.id.asc())
            )
            return list(result.scalars())

    async def add_job(self, job_id: str, experiment_id: str, group_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                group = await session.get(ExperimentGroup, group_id)
                if group is None or group.experiment_id != experiment_id:
                    raise ExperimentGroupNotFoundError(
                        "experiment group not found for this experiment"
                    )
                session.add(
                    ExperimentJob(
                        job_id=job_id,
                        experiment_id=experiment_id,
                        group_id=group_id,
                    )
                )
                await session.flush()

    async def list_jobs(self, experiment_id: str) -> list[ExperimentJob]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentJob)
                .where(ExperimentJob.experiment_id == experiment_id)
                .order_by(ExperimentJob.created_at.asc(), ExperimentJob.job_id.asc())
            )
            return list(result.scalars())

    async def list_job_ids(self, experiment_id: str) -> list[str]:
        return [entry.job_id for entry in await self.list_jobs(experiment_id)]

    async def group_for_job(self, job_id: str) -> ExperimentGroup | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentGroup)
                .join(ExperimentJob, ExperimentJob.group_id == ExperimentGroup.id)
                .where(ExperimentJob.job_id == job_id)
            )
            return result.scalar_one_or_none()

    # --- E4 failure samples -----------------------------------------------------

    async def record_failure(
        self,
        *,
        job_id: str,
        experiment_id: str | None = None,
        prompt_version_id: str | None = None,
        reason: str | None = None,
        qc_issues_json: list[dict[str, Any]] | None = None,
    ) -> FailureSample:
        sample = FailureSample(
            id=str(uuid.uuid4()),
            job_id=job_id,
            experiment_id=experiment_id,
            prompt_version_id=prompt_version_id,
            reason=reason,
            qc_issues_json=qc_issues_json,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(sample)
                await session.flush()
                return sample

    async def list_failures_for_experiment(self, experiment_id: str) -> list[FailureSample]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FailureSample)
                .where(FailureSample.experiment_id == experiment_id)
                .order_by(FailureSample.created_at.desc())
            )
            return list(result.scalars())

    async def list_failures_for_prompt(self, prompt_version_id: str) -> list[FailureSample]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FailureSample)
                .where(FailureSample.prompt_version_id == prompt_version_id)
                .order_by(FailureSample.created_at.desc())
            )
            return list(result.scalars())
