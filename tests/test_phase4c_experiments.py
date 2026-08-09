"""Phase 04-C E1 experiment definition and grouping tests (repository level)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pixelle_video.experiments.repository import (
    ExperimentGroupNotFoundError,
    ExperimentNotFoundError,
    ExperimentRepository,
)
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'e1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ExperimentRepository(factory)
    job_repository = MediaJobRepository(factory)
    try:
        yield factory, repository, job_repository
    finally:
        await engine.dispose()


def _groups() -> list[dict]:
    return [
        {
            "group_name": "对照组",
            "prompt_version_id": "pv-1",
            "model_name": "deepseek",
            "params_json": {"temperature": 0.7},
        },
        {
            "group_name": "实验组V4",
            "prompt_version_id": "pv-2",
            "model_name": "deepseek",
            "params_json": {"temperature": 0.9},
        },
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


async def test_repository_create_experiment_with_groups(env) -> None:
    _factory, repository, _jobs = env
    experiment = await repository.create_experiment(
        name="标题测试", metric="composite", groups=_groups()
    )
    assert experiment.name == "标题测试"
    groups = await repository.list_groups(experiment.id)
    names = {group.group_name for group in groups}
    assert names == {"对照组", "实验组V4"}
    by_name = {group.group_name: group for group in groups}
    assert by_name["对照组"].prompt_version_id == "pv-1"


async def test_repository_create_default_metric_and_status(env) -> None:
    _factory, repository, _jobs = env
    experiment = await repository.create_experiment(name="默认实验", groups=[])
    assert experiment.metric == "composite"
    assert experiment.status == "active"


async def test_repository_get_and_list(env) -> None:
    _factory, repository, _jobs = env
    experiment = await repository.create_experiment(name="A", groups=_groups())
    fetched = await repository.get_experiment(experiment.id)
    assert fetched is not None and fetched.status == "active"
    rows, _ = await repository.list_experiments(limit=10, offset=0)
    assert len(rows) == 1


async def test_repository_list_filters_by_status(env) -> None:
    _factory, repository, _jobs = env
    await repository.create_experiment(name="A", groups=[])
    await repository.create_experiment(name="B", groups=[])
    archived = await repository.create_experiment(name="C", groups=[])
    await repository.update_status(archived.id, "archived")
    active_rows, _ = await repository.list_experiments(status="active", limit=10, offset=0)
    assert len(active_rows) == 2
    archived_rows, _ = await repository.list_experiments(status="archived", limit=10, offset=0)
    assert len(archived_rows) == 1


async def test_repository_update_status_and_missing(env) -> None:
    _factory, repository, _jobs = env
    experiment = await repository.create_experiment(name="A", groups=[])
    updated = await repository.update_status(experiment.id, "completed")
    assert updated.status == "completed"
    with pytest.raises(ExperimentNotFoundError):
        await repository.update_status("missing", "completed")


async def test_repository_add_job_and_list(env) -> None:
    _factory, repository, jobs = env
    experiment = await repository.create_experiment(name="A", groups=_groups())
    groups = {group.group_name: group for group in await repository.list_groups(experiment.id)}
    created = (await jobs.create_job(_job_create())).job
    await repository.add_job(created.job_id, experiment.id, groups["对照组"].id)
    entries = await repository.list_jobs(experiment.id)
    assert len(entries) == 1
    assert entries[0].group_id == groups["对照组"].id
    group = await repository.group_for_job(created.job_id)
    assert group is not None and group.group_name == "对照组"


async def test_repository_add_job_wrong_group_raises(env) -> None:
    _factory, repository, jobs = env
    experiment = await repository.create_experiment(name="A", groups=_groups())
    created = (await jobs.create_job(_job_create())).job
    with pytest.raises(ExperimentGroupNotFoundError):
        await repository.add_job(created.job_id, experiment.id, "wrong-group")


async def test_repository_group_for_job_missing(env) -> None:
    _factory, repository, jobs = env
    created = (await jobs.create_job(_job_create())).job
    group = await repository.group_for_job(created.job_id)
    assert group is None
