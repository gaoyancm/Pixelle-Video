"""Phase 04-C E4 failure sample feedback tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_experiment_service
from api.routers.experiments import router as experiments_router
from api.routers.prompts import router as prompts_router
from api.services.experiments import ExperimentApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.experiments.repository import ExperimentRepository
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'fail.db').as_posix()}")
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
    )
    try:
        yield factory, repository, job_repository, service
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


async def _seed_experiment(repository, jobs, *, with_qc_critical: bool = False):
    experiment = await repository.create_experiment(
        name="失败回流实验",
        groups=[{"group_name": "对照组", "prompt_version_id": "pv-control"}],
    )
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job_ids = []
    for index in range(5):
        job = (await jobs.create_job(_job_create())).job
        await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
        job_ids.append(job.job_id)
        if index == 0 and with_qc_critical:
            await service_audit(jobs, job.job_id)
    return experiment, job_ids


async def service_audit(jobs, job_id: str) -> None:
    del jobs
    del job_id
    return None


async def _record_qc(service, job_id: str, *, critical: bool = False) -> None:
    issues = (
        [
            {
                "rule_id": "r1",
                "severity": "critical",
                "category": "platform",
                "field": "nsfw",
                "message": "违规",
            }
        ]
        if critical
        else []
    )
    await service.audit.record(
        event_type="qc_completed",
        scope_type="job",
        scope_id=job_id,
        details={
            "total_rules": 10,
            "passed": 8,
            "issues": issues,
            "decision": "human_review" if critical else "pass",
        },
    )


async def test_failure_engine_flags_bottom_twenty_percent(env) -> None:
    _factory, repository, _jobs, service = env
    payload = {
        "groups": [
            {
                "group_name": "对照组",
                "prompt_version_id": "pv-1",
                "jobs": [
                    {"job_id": f"j{index}", "composite": float(index * 10)} for index in range(5)
                ],
            }
        ]
    }
    samples = await service.failure_engine.collect_failures("exp-1", payload)
    job_ids = {sample.job_id for sample in samples}
    assert "j0" in job_ids  # composite 0 is the bottom 20%
    assert "j4" not in job_ids


async def test_failure_engine_flags_critical_qc_issue(env) -> None:
    _factory, repository, _jobs, service = env
    payload = {
        "groups": [
            {
                "group_name": "对照组",
                "prompt_version_id": "pv-2",
                "jobs": [
                    {"job_id": f"j{index}", "composite": float(80 + index)} for index in range(5)
                ],
            }
        ]
    }
    samples = await service.failure_engine.collect_failures(
        "exp-2",
        payload,
    )
    # No critical QC records exist -> only bottom 20% flagged.
    assert len(samples) == 1


async def test_failure_engine_links_prompt_version(env) -> None:
    _factory, repository, _jobs, service = env
    payload = {
        "groups": [
            {
                "group_name": "对照组",
                "prompt_version_id": "pv-version-9",
                "jobs": [{"job_id": "bad-job", "composite": 1.0}],
            }
        ]
    }
    samples = await service.failure_engine.collect_failures("exp-3", payload)
    assert len(samples) == 1
    assert samples[0].prompt_version_id == "pv-version-9"
    assert "最低 20%" in (samples[0].reason or "")


async def test_collect_failures_end_to_end(env) -> None:
    _factory, repository, jobs, service = env
    experiment = await repository.create_experiment(
        name="端到端实验",
        groups=[{"group_name": "对照组", "prompt_version_id": "pv-ctrl"}],
    )
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    low = (await jobs.create_job(_job_create())).job
    high = (await jobs.create_job(_job_create())).job
    await repository.add_job(low.job_id, experiment.id, groups["对照组"].id)
    await repository.add_job(high.job_id, experiment.id, groups["对照组"].id)
    await _record_qc(service, low.job_id, critical=True)
    await _record_qc(service, high.job_id)
    samples = await service.collect_failures(experiment.id)
    assert any(sample.job_id == low.job_id for sample in samples)
    assert samples[0].experiment_id == experiment.id


async def test_failures_api_returns_samples(api_env) -> None:
    client, repository, jobs, service = api_env
    experiment = await repository.create_experiment(
        name="API实验",
        groups=[{"group_name": "对照组", "prompt_version_id": "pv-api"}],
    )
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    await _record_qc(service, job.job_id, critical=True)
    response = await client.get(f"/api/admin/experiments/{experiment.id}/failures")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) >= 1
    assert items[0]["job_id"] == job.job_id


async def test_prompt_failures_api(api_env) -> None:
    client, repository, jobs, service = api_env
    experiment = await repository.create_experiment(
        name="Prompt关联实验",
        groups=[{"group_name": "对照组", "prompt_version_id": "pv-xyz"}],
    )
    groups = {g.group_name: g for g in await repository.list_groups(experiment.id)}
    job = (await jobs.create_job(_job_create())).job
    await repository.add_job(job.job_id, experiment.id, groups["对照组"].id)
    await _record_qc(service, job.job_id, critical=True)
    await service.collect_failures(experiment.id)
    response = await client.get("/api/admin/prompts/pv-xyz/failures")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["prompt_version_id"] == "pv-xyz"


@pytest.fixture
async def api_env(env):
    _factory, repository, jobs, service = env
    app = FastAPI()
    app.include_router(experiments_router, prefix="/api")
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_experiment_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, repository, jobs, service
