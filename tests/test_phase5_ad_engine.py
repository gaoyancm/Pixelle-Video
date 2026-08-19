"""Phase 05 A2 ad production engine tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_product_service
from api.routers.products import router as products_router
from api.services.products import ProductApplicationService
from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.products.ad_engine import AdProductionEngine
from pixelle_video.products.models import ProductBrief
from pixelle_video.products.repository import ProductBriefRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    job_repository = MediaJobRepository(factory)
    management_repository = ManagementRepository(factory)
    await management_repository.create_project(name="Test project", project_id="project-x")
    asset_repository = AssetRepository(factory)
    ad_engine = AdProductionEngine(
        brief_repository, management_repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    service = ProductApplicationService(
        brief_repository,
        ad_engine=ad_engine,
        asset_repository=asset_repository,
        job_repository=job_repository,
    )
    try:
        yield factory, brief_repository, job_repository, management_repository, service, ad_engine
    finally:
        await engine.dispose()


async def _brief(repository: ProductBriefRepository) -> ProductBrief:
    return await repository.create_brief(
        product_name="手工皮具钱包",
        description="意大利头层牛皮手工缝制",
        selling_points=["头层牛皮", "手工缝制"],
        platforms=["etsy", "tiktok"],
        project_id="project-x",
    )


async def test_plan_production_creates_image_video_caption_tasks(env) -> None:
    _factory, repository, _jobs, _management, _service, engine = env
    brief = await _brief(repository)
    tasks = engine.plan_production(brief)
    kinds = [task["kind"] for task in tasks]
    assert kinds.count("image") == 3  # 1 main + 2 scenes
    assert kinds.count("video") == 2  # etsy + tiktok
    assert kinds.count("caption") == 2
    roles = {task["role"] for task in tasks}
    assert "main_image" in roles
    assert "ad_video_tiktok" in roles
    assert "caption_etsy" in roles


async def test_plan_production_defaults_platforms_when_empty(env) -> None:
    _factory, repository, _jobs, _management, _service, engine = env
    brief = await repository.create_brief(
        product_name="默认平台", description="默认平台测试", project_id="project-x"
    )
    tasks = engine.plan_production(brief)
    video_platforms = {task["platform"] for task in tasks if task["kind"] == "video"}
    assert video_platforms == {"etsy", "tiktok"}


async def test_start_production_creates_batch_and_jobs(env) -> None:
    _factory, repository, job_repository, management_repository, _service, engine = env
    brief = await _brief(repository)
    payload = await engine.start_production(brief.id)
    assert payload["brief_id"] == brief.id
    assert payload["batch_id"]
    assert payload["tasks"] == 7  # 3 images + 2 videos + 2 captions
    assert len(payload["jobs"]["image"]) == 3
    assert len(payload["jobs"]["video"]) == 2
    assert len(payload["jobs"]["caption"]) == 2
    brief = await repository.get_brief(brief.id)
    assert brief.status == "processing"


async def test_start_production_jobs_are_persisted(env) -> None:
    _factory, repository, job_repository, _management, _service, engine = env
    brief = await _brief(repository)
    payload = await engine.start_production(brief.id)
    for kind in ("image", "video", "caption"):
        for job_id in payload["jobs"][kind]:
            job = await job_repository.get_job(job_id)
            assert job is not None
            assert job.input_json["brief_id"] == brief.id
            if job.executor_kind == "private_comfyui":
                assert job.node_id == "test-node"
                assert job.input_json["prompt"]
                assert "prompt_hint" not in job.input_json
            else:
                assert job.node_id is None


async def test_no_node_rejects_before_batch_or_job_write(env) -> None:
    _factory, repository, job_repository, management_repository, _service, _engine = env
    brief = await _brief(repository)
    blocked = AdProductionEngine(repository, management_repository, job_repository)
    from pixelle_video.media_jobs import MediaJobsDisabledError

    with pytest.raises(MediaJobsDisabledError):
        await blocked.start_production(brief.id)
    assert await job_repository.list_jobs() == []
    assert await management_repository.list_batches(project_id="project-x") == []


async def test_start_production_missing_brief_raises(env) -> None:
    _factory, _repository, _jobs, _management, _service, engine = env
    from pixelle_video.products.repository import ProductBriefNotFoundError

    with pytest.raises(ProductBriefNotFoundError):
        await engine.start_production("missing-brief")


async def test_start_production_without_project_rejects_before_writes(env) -> None:
    _factory, repository, job_repository, management_repository, _service, engine = env
    brief = await repository.create_brief(product_name="无项目", description="不能进入生产")
    from pixelle_video.products.ad_engine import ProductProjectRequiredError

    with pytest.raises(ProductProjectRequiredError):
        await engine.start_production(brief.id)
    assert await job_repository.list_jobs() == []
    assert await management_repository.list_batches(project_id="default-project") == []


async def test_service_progress_tracks_job_states(env) -> None:
    _factory, repository, job_repository, _management, service, engine = env
    brief = await _brief(repository)
    await engine.start_production(brief.id)
    progress = await service.progress(brief.id)
    assert progress["brief_id"] == brief.id
    assert progress["total_jobs"] == 7
    assert progress["completed_jobs"] == 0
    assert progress["jobs_by_kind"]["image"] == 3
    assert progress["jobs_by_kind"]["video"] == 2


async def test_service_results_groups_by_platform(env) -> None:
    _factory, repository, job_repository, _management, service, engine = env
    brief = await _brief(repository)
    await engine.start_production(brief.id)
    results = await service.results(brief.id)
    assert results["brief_id"] == brief.id
    keys = set(results["groups"].keys())
    assert "main_image" in keys
    assert "etsy" in keys
    assert "tiktok" in keys


@pytest.fixture
async def api_client(env):
    _factory, _repository, _jobs, _management, service, _engine = env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_confirm_triggers_production(api_client) -> None:
    client, _factory, repository, service = api_client
    brief = await repository.create_brief(
        product_name="确认测试",
        description="确认流程测试",
        platforms=["tiktok"],
        project_id="project-x",
    )
    response = await client.post(f"/api/products/briefs/{brief.id}/confirm")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["batch_id"]
    assert len(body["jobs"]["image"]) == 3


async def test_api_progress_and_results(api_client) -> None:
    client, _factory, repository, service = api_client
    brief = await repository.create_brief(
        product_name="进度测试",
        description="进度流程测试",
        platforms=["instagram"],
        project_id="project-x",
    )
    await client.post(f"/api/products/briefs/{brief.id}/confirm")
    progress = await client.get(f"/api/products/briefs/{brief.id}/progress")
    assert progress.status_code == 200
    assert progress.json()["total_jobs"] == 5  # 3 images + 1 video + 1 caption
    results = await client.get(f"/api/products/briefs/{brief.id}/results")
    assert results.status_code == 200
    assert "instagram" in results.json()["groups"]


async def test_plan_production_caption_tasks_carry_platform(env) -> None:
    _factory, repository, _jobs, _management, _service, engine = env
    brief = await _brief(repository)
    tasks = engine.plan_production(brief)
    captions = [task for task in tasks if task["kind"] == "caption"]
    assert {task["platform"] for task in captions} == {"etsy", "tiktok"}
    assert all("广告文案" in task["prompt"] for task in captions)


async def test_service_progress_counts_completed_jobs(env) -> None:
    _factory, repository, job_repository, _management, service, engine = env
    brief = await _brief(repository)
    payload = await engine.start_production(brief.id)
    # Mark one image job as succeeded.
    first_image = payload["jobs"]["image"][0]
    from sqlalchemy import update

    from pixelle_video.media_jobs.models import MediaJob

    async with job_repository._session_factory() as session:
        async with session.begin():
            await session.execute(
                update(MediaJob).where(MediaJob.job_id == first_image).values(status="succeeded")
            )
    progress = await service.progress(brief.id)
    assert progress["completed_jobs"] == 1
    assert progress["running_jobs"] == 6


async def test_api_progress_tracks_running_state(api_client) -> None:
    client, _factory, repository, service = api_client
    brief = await repository.create_brief(
        product_name="运行态测试",
        description="运行状态测试",
        platforms=["meta"],
        project_id="project-x",
    )
    await client.post(f"/api/products/briefs/{brief.id}/confirm")
    progress = await client.get(f"/api/products/briefs/{brief.id}/progress")
    assert progress.status_code == 200
    body = progress.json()
    assert body["status"] == "processing"
    assert body["running_jobs"] == 5  # 3 images + 1 video + 1 caption
