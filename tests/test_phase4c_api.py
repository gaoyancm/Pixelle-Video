"""Phase 04-C experiment API integration tests (E1-E3 endpoints)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_experiment_service
from api.routers.experiments import router as experiments_router
from api.services.experiments import ExperimentApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.experiments.repository import ExperimentRepository
from pixelle_video.experiments.stats import ExperimentStats
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ExperimentRepository(factory)
    job_repository = MediaJobRepository(factory)
    audit = AuditRepository(factory)
    service = ExperimentApplicationService(
        repository,
        job_lookup=job_repository.get_job,
        audit=audit,
        size_lookup=lambda job_id: 12345,
        stats=ExperimentStats(),
    )
    try:
        yield factory, repository, job_repository, service
    finally:
        await engine.dispose()


def _groups() -> list[dict]:
    return [
        {"group_name": "对照组", "prompt_version_id": "pv-1"},
        {"group_name": "实验组V4", "prompt_version_id": "pv-2"},
    ]


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


async def _finish_job(job_repository: MediaJobRepository, job_id: str, *, cost: float) -> None:
    from sqlalchemy import update

    from pixelle_video.media_jobs.models import MediaJob

    async with job_repository._session_factory() as session:
        async with session.begin():
            await session.execute(
                update(MediaJob)
                .where(MediaJob.job_id == job_id)
                .values(
                    status="succeeded",
                    started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
                    actual_cost=cost,
                )
            )


@pytest.fixture
async def api_client(env):
    _factory, _repository, _jobs, service = env
    app = FastAPI()
    app.include_router(experiments_router, prefix="/api")
    app.dependency_overrides[get_experiment_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, _jobs, service


async def test_api_create_and_list(api_client) -> None:
    client, _factory, _repository, _jobs, _service = api_client
    created = await client.post(
        "/api/admin/experiments",
        json={"name": "标题实验", "metric": "composite", "groups": _groups()},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "标题实验"

    listing = await client.get("/api/admin/experiments")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == body["id"]


async def test_api_detail_and_update(api_client) -> None:
    client, _factory, _repository, _jobs, _service = api_client
    created = (
        await client.post(
            "/api/admin/experiments",
            json={"name": "标题实验", "groups": _groups()},
        )
    ).json()
    detail = await client.get(f"/api/admin/experiments/{created['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["groups"]) == 2

    updated = await client.patch(
        f"/api/admin/experiments/{created['id']}", json={"status": "completed"}
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "completed"


async def test_api_metrics_and_jobs(api_client) -> None:
    client, _factory, repository, jobs, _service = api_client
    experiment = await repository.create_experiment(name="A", groups=_groups())
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await _finish_job(jobs, job.job_id, cost=5.0)
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)

    metrics = await client.get(f"/api/admin/experiments/{experiment.id}/metrics")
    assert metrics.status_code == 200
    groups_metrics = {group["group_name"]: group for group in metrics.json()["groups"]}
    assert groups_metrics["对照组"]["sample_size"] == 1

    jobs_response = await client.get(f"/api/admin/experiments/{experiment.id}/jobs")
    assert jobs_response.status_code == 200
    assert jobs_response.json()["items"][0]["job_id"] == job.job_id


async def test_api_report_structure(api_client) -> None:
    client, _factory, repository, jobs, _service = api_client
    experiment = await repository.create_experiment(
        name="报告实验", groups=[{"group_name": "对照组"}, {"group_name": "实验组"}]
    )
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    for _ in range(5):
        job = (await jobs.create_job(_job_create())).job
        await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    response = await client.get(f"/api/admin/experiments/{experiment.id}/report")
    assert response.status_code == 200
    body = response.json()
    assert body["experiment_id"] == experiment.id
    assert body["metric"] == "composite"
    assert isinstance(body["groups"], list)
    assert "confidence" in body


async def test_api_not_found(api_client) -> None:
    client, _factory, _repository, _jobs, _service = api_client
    response = await client.get("/api/admin/experiments/missing")
    assert response.status_code == 404
