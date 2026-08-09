"""Phase 04-C E2 experiment metrics tests (engine level)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pixelle_video.experiments.metrics import ExperimentMetrics
from pixelle_video.experiments.repository import ExperimentRepository
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'e2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ExperimentRepository(factory)
    job_repository = MediaJobRepository(factory)

    qc_records: dict[str, dict] = {}

    async def qc_reader(job_id: str) -> dict | None:
        return qc_records.get(job_id)

    metrics_engine = ExperimentMetrics(
        repository,
        job_lookup=job_repository.get_job,
        qc_reader=qc_reader,
        size_lookup=lambda job_id: 12345,
    )
    try:
        yield factory, repository, job_repository, metrics_engine, qc_records
    finally:
        await engine.dispose()


def _job_create() -> MediaJobCreate:
    return MediaJobCreate(
        workflow_type="a800_wan22_t2v_33f",
        workflow_key="workflow.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id="a800",
        input_json={"prompt": "safe prompt"},
        input_assets_json=[],
        idempotency_key=None,
    )


async def _finish_job(
    job_repository: MediaJobRepository,
    job_id: str,
    *,
    started: datetime,
    finished: datetime,
    cost: float,
) -> None:
    from sqlalchemy import update

    from pixelle_video.media_jobs.models import MediaJob

    async with job_repository._session_factory() as session:
        async with session.begin():
            await session.execute(
                update(MediaJob)
                .where(MediaJob.job_id == job_id)
                .values(
                    status="succeeded",
                    started_at=started,
                    finished_at=finished,
                    actual_cost=cost,
                )
            )


async def test_metrics_computes_all_five_signals(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(
        name="A", groups=[{"group_name": "对照组", "prompt_version_id": "pv-1"}]
    )
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await _finish_job(
        jobs,
        job.job_id,
        started=datetime(2026, 8, 1, tzinfo=timezone.utc),
        finished=datetime(2026, 8, 1, 0, 1, 0, tzinfo=timezone.utc),
        cost=10.0,
    )
    qc_records[job.job_id] = {"total_rules": 10, "passed": 8}
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    assert group["sample_size"] == 1
    job_metrics = group["jobs"][0]
    assert job_metrics["duration_seconds"] == 60.0
    assert job_metrics["cost"] == 10.0
    assert job_metrics["qc_score"] == 0.8
    assert job_metrics["size_bytes"] == 12345


async def test_metrics_composite_max_score_when_all_optimal(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(name="A", groups=[{"group_name": "对照组"}])
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await _finish_job(
        jobs,
        job.job_id,
        started=datetime(2026, 8, 1, tzinfo=timezone.utc),
        finished=datetime(2026, 8, 1, 0, 0, 30, tzinfo=timezone.utc),
        cost=1.0,
    )
    qc_records[job.job_id] = {"total_rules": 10, "passed": 10}
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    assert group["jobs"][0]["composite"] == 100.0


async def test_metrics_missing_qc_yields_none_score(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(name="A", groups=[{"group_name": "对照组"}])
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await _finish_job(
        jobs,
        job.job_id,
        started=datetime(2026, 8, 1, tzinfo=timezone.utc),
        finished=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
        cost=10.0,
    )
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    job_metrics = group["jobs"][0]
    assert job_metrics["qc_score"] is None
    assert job_metrics["composite"] < 100.0


async def test_metrics_cost_component_penalizes_expensive_job(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(name="A", groups=[{"group_name": "对照组"}])
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    cheap = (await jobs.create_job(_job_create())).job
    expensive = (await jobs.create_job(_job_create())).job
    for job, cost in ((cheap, 10.0), (expensive, 100.0)):
        await _finish_job(
            jobs,
            job.job_id,
            started=datetime(2026, 8, 1, tzinfo=timezone.utc),
            finished=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
            cost=cost,
        )
        await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    scores = {job["job_id"]: job["composite"] for job in group["jobs"]}
    assert scores[cheap.job_id] > scores[expensive.job_id]


async def test_metrics_qc_score_ratio(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(name="A", groups=[{"group_name": "对照组"}])
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await _finish_job(
        jobs,
        job.job_id,
        started=datetime(2026, 8, 1, tzinfo=timezone.utc),
        finished=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
        cost=10.0,
    )
    qc_records[job.job_id] = {"total_rules": 10, "passed": 5}
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    assert group["jobs"][0]["qc_score"] == 0.5


async def test_metrics_time_component_penalizes_slow_job(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(name="A", groups=[{"group_name": "对照组"}])
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    fast = (await jobs.create_job(_job_create())).job
    slow = (await jobs.create_job(_job_create())).job
    for job, duration_minutes in ((fast, 1), (slow, 10)):
        await _finish_job(
            jobs,
            job.job_id,
            started=datetime(2026, 8, 1, tzinfo=timezone.utc),
            finished=datetime(2026, 8, 1, 0, duration_minutes, tzinfo=timezone.utc),
            cost=50.0,
        )
        await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    scores = {job["job_id"]: job["composite"] for job in group["jobs"]}
    assert scores[fast.job_id] > scores[slow.job_id]


async def test_metrics_multiple_jobs_aggregate_per_group(env) -> None:
    _factory, repository, jobs, metrics_engine, qc_records = env
    experiment = await repository.create_experiment(name="A", groups=[{"group_name": "对照组"}])
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    for _ in range(3):
        job = (await jobs.create_job(_job_create())).job
        await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    payload = await metrics_engine.compute_experiment(experiment.id)
    group = next(g for g in payload["groups"] if g["group_name"] == "对照组")
    assert group["sample_size"] == 3
    assert len(group["jobs"]) == 3
