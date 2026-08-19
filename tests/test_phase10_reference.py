"""Phase 10 reference-image wiring tests.

Covers:
- 05: plan metadata moved off reference_images_json onto a dedicated plan_id
  column, freeing reference_images_json for real reference imagery.
- 05: ad video tasks become first-frame I2V when a reference image is present.
- 06: short-video reference_image_id passthrough to first-frame I2V.
- API: from-plan endpoints accept a reference image / reference_image_id body.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_product_service, get_video_service
from api.routers.products import router as products_router
from api.routers.videos import router as videos_router
from api.services.products import ProductApplicationService
from api.services.videos import VideoApplicationService
from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_assets.contracts import new_asset_id
from pixelle_video.media_assets.models import MediaAsset
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.products.ad_engine import I2V_WORKFLOW, IMG2IMG_WORKFLOW, AdProductionEngine
from pixelle_video.products.brief_mapper import BriefMapper
from pixelle_video.products.repository import ProductBriefRepository
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine
from pixelle_video.videos.script_mapper import ScriptMapper
from pixelle_video.videos.storyboard import I2V_WORKFLOW as STORYBOARD_I2V_WORKFLOW
from pixelle_video.videos.storyboard import StoryboardEngine


async def _make_input_asset(asset_repository: AssetRepository, asset_id: str | None = None) -> str:
    """Persist an available INPUT image asset and return its id."""
    aid = asset_id or new_asset_id()
    asset = MediaAsset(
        id=aid,
        kind="input",
        state="available",
        backend="local",
        object_key=f"inputs/{aid}.png",
        original_filename="ref.png",
        media_type="image",
        mime_type="image/png",
        size_bytes=10,
        sha256="a" * 64,
        source="upload",
    )
    persisted, _ = await asset_repository.create(asset)
    return persisted.id


# ── products fixtures ────────────────────────────────────────────────────────


@pytest.fixture
async def product_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'p10_products.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    job_repository = MediaJobRepository(factory)
    management_repository = ManagementRepository(factory)
    await management_repository.create_project(
        name="Phase 10 reference project", project_id="phase10-reference-project"
    )
    asset_repository = AssetRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    ad_engine = AdProductionEngine(
        brief_repository, management_repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    service = ProductApplicationService(
        brief_repository,
        ad_engine=ad_engine,
        asset_repository=asset_repository,
        job_repository=job_repository,
        plan_repository=plan_repository,
        brief_mapper=BriefMapper(),
    )
    try:
        yield (
            factory,
            brief_repository,
            job_repository,
            asset_repository,
            plan_repository,
            service,
            ad_engine,
        )
    finally:
        await engine.dispose()


async def _product_plan(plan_repository: ContentPlanRepository):
    return await plan_repository.create_plan(
        request_text="手工皮具钱包，Etsy主图+TikTok广告",
        intent="product_ad",
        plan_json={
            "summary": "为手工皮具钱包生成Etsy主图和TikTok广告",
            "target_audience": "25-45岁追求品质的女性",
            "platforms": ["etsy", "tiktok"],
            "creative_directions": [{"hook": "意大利头层牛皮", "angle": "品质感奢华风"}],
            "visual_style": {"palette": "温暖皮革色系", "mood": "精致"},
        },
    )


async def test_05_plan_id_frees_reference_images(product_env) -> None:
    _f, brief_repository, _j, _a, plan_repository, service, _e = product_env
    plan = await _product_plan(plan_repository)
    payload = await service.create_brief_from_plan(plan.id)
    brief = await brief_repository.get_brief(payload["brief_id"])
    assert brief.plan_id == plan.id
    assert brief.reference_images_json is None or brief.reference_images_json == []
    # plan trace still works via plan_id
    traced = await service.get_plan_for_brief(brief.id)
    assert traced is not None and traced["plan_id"] == plan.id


async def test_05_video_tasks_become_i2v_with_reference(product_env) -> None:
    _f, brief_repository, _j, asset_repository, _p, _s, engine = product_env
    asset_id = await _make_input_asset(asset_repository)
    brief = await brief_repository.create_brief(
        product_name="手工皮具钱包",
        description="意大利头层牛皮",
        platforms=["etsy", "tiktok"],
        reference_images=[asset_id],
        project_id="phase10-reference-project",
    )
    tasks = engine.plan_production(brief)
    for task in tasks:
        if task["kind"] == "video":
            assert task["workflow_type"] == I2V_WORKFLOW
            assert task["reference_images"] == [asset_id]
        elif task["kind"] == "image":
            assert task["workflow_type"] == IMG2IMG_WORKFLOW
            assert task["reference_images"] == [asset_id]
        else:
            assert task.get("reference_images", []) == []


async def test_05_start_production_links_input_asset(product_env) -> None:
    _f, brief_repository, job_repository, asset_repository, _p, _s, engine = product_env
    asset_id = await _make_input_asset(asset_repository)
    brief = await brief_repository.create_brief(
        product_name="手工皮具钱包",
        description="意大利头层牛皮",
        platforms=["etsy", "tiktok"],
        reference_images=[asset_id],
        project_id="phase10-reference-project",
    )
    payload = await engine.start_production(brief.id)
    for job_id in payload["jobs"]["video"]:
        job = await job_repository.get_job(job_id)
        assert job.workflow_type == I2V_WORKFLOW
        assert job.input_assets_json == [{"asset_id": asset_id, "role": "input_image"}]
        # relation persisted (input asset resolvable for the executor)
        assets = await asset_repository.input_assets_for_job(job_id)
        assert [a.id for a in assets] == [asset_id]


# ── videos fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def video_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'p10_videos.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    job_repository = MediaJobRepository(factory)
    asset_repository = AssetRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    script_engine = ScriptEngine(repository)
    storyboard_engine = StoryboardEngine(
        repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    service = VideoApplicationService(
        repository,
        script_engine=script_engine,
        storyboard_engine=storyboard_engine,
        job_repository=job_repository,
        plan_repository=plan_repository,
        script_mapper=ScriptMapper(),
    )
    try:
        yield (
            factory,
            repository,
            job_repository,
            asset_repository,
            plan_repository,
            service,
            storyboard_engine,
        )
    finally:
        await engine.dispose()


async def test_06_reference_image_passthrough(video_env) -> None:
    _f, repository, _j, asset_repository, _p, _s, _e = video_env
    asset_id = await _make_input_asset(asset_repository)
    payload = await ScriptEngine(repository).generate_script(
        topic="人工智能如何改变日常生活",
        target_duration=60,
        reference_image_id=asset_id,
    )
    script = await repository.get_script(payload["id"])
    assert script.reference_image_id == asset_id


async def test_06_video_frames_become_i2v_with_reference(video_env) -> None:
    _f, repository, job_repository, asset_repository, _p, _s, engine = video_env
    asset_id = await _make_input_asset(asset_repository)
    script_id = (
        await ScriptEngine(repository).generate_script(
            topic="人工智能如何改变日常生活",
            target_duration=60,
            reference_image_id=asset_id,
        )
    )["id"]
    await engine.build_storyboard(script_id)
    payload = await engine.generate_assets(script_id)
    # frame 3 is the motion/video frame (index % 3 == 0)
    video_job_id = payload["jobs"]["3"]
    job = await job_repository.get_job(video_job_id)
    assert job.workflow_type == STORYBOARD_I2V_WORKFLOW
    assert job.input_assets_json == [{"asset_id": asset_id, "role": "input_image"}]
    assets = await asset_repository.input_assets_for_job(video_job_id)
    assert [a.id for a in assets] == [asset_id]


async def test_06_no_reference_keeps_t2v(video_env) -> None:
    _f, repository, job_repository, _a, _p, _s, engine = video_env
    script_id = (
        await ScriptEngine(repository).generate_script(
            topic="人工智能如何改变日常生活", target_duration=60
        )
    )["id"]
    await engine.build_storyboard(script_id)
    payload = await engine.generate_assets(script_id)
    job = await job_repository.get_job(payload["jobs"]["3"])
    assert job.workflow_type != STORYBOARD_I2V_WORKFLOW


# ── API from-plan passthrough ────────────────────────────────────────────────


@pytest.fixture
async def api_client(product_env, video_env):
    _pf, _br, _jr, _ar, product_plan_repo, product_service, _ae = product_env
    _vf, _vr, _vjr, _var, video_plan_repo, video_service, _ve = video_env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.include_router(videos_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: product_service
    app.dependency_overrides[get_video_service] = lambda: video_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, product_plan_repo, video_plan_repo, _ar


async def test_api_products_from_plan_accepts_reference(api_client) -> None:
    client, plan_repository, _vr, asset_repository = api_client
    plan = await _product_plan(plan_repository)
    asset_id = await _make_input_asset(asset_repository)
    created = await client.post(
        f"/api/products/briefs/from-plan/{plan.id}",
        json={"reference_images": [asset_id]},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["plan_id"] == plan.id
    assert body.get("reference_images") == [asset_id]


async def test_api_videos_from_plan_accepts_reference(api_client) -> None:
    client, _pr, video_plan_repository, asset_repository = api_client
    # a short_video plan is required by the videos from-plan endpoint
    plan = await video_plan_repository.create_plan(
        request_text="人工智能改变日常生活的 5 种方式",
        intent="short_video",
        plan_json={"summary": "短视频脚本", "platforms": ["tiktok"]},
    )
    asset_id = await _make_input_asset(asset_repository)
    created = await client.post(
        f"/api/videos/scripts/from-plan/{plan.id}",
        json={"reference_image_id": asset_id},
    )
    assert created.status_code == 201
