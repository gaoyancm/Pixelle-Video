"""Phase 06-F B1 plan-to-script mapping tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_video_service
from api.routers.videos import router as videos_router
from api.services.videos import VideoApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_mapper import ScriptMapper


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'b1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    script_repository = VideoScriptRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    service = VideoApplicationService(
        script_repository,
        plan_repository=plan_repository,
        script_mapper=ScriptMapper(),
    )
    try:
        yield factory, script_repository, plan_repository, service
    finally:
        await engine.dispose()


async def _make_plan(plan_repository: ContentPlanRepository, **extra):
    return await plan_repository.create_plan(
        request_text="人工智能科普短视频，讲讲大模型",
        intent="short_video",
        plan_json={
            "intent": "short_video",
            "summary": "人工智能大模型科普，3个要点讲清楚",
            "target_audience": "对AI好奇的年轻人",
            "platforms": ["tiktok"],
            "visual_style": {"palette": "科技蓝", "mood": "未来感"},
            "creative_directions": [{"hook": "大模型是什么"}],
            "tasks": [{"type": "video", "template": "short_video", "count": 4}],
        },
        **extra,
    )


async def test_mapper_maps_topic(env) -> None:
    _factory, _scripts, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = ScriptMapper().map(plan)
    assert mapped["topic"] == "人工智能科普短视频"
    assert mapped["platform"] == "tiktok"
    assert mapped["target_duration"] == 60


async def test_mapper_injects_hook_from_audience(env) -> None:
    _factory, _scripts, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = ScriptMapper().map(plan)
    assert mapped["script_json"]["hook"] == "对AI好奇的年轻人"


async def test_mapper_sets_visual_direction_per_scene(env) -> None:
    _factory, _scripts, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = ScriptMapper().map(plan)
    scenes = mapped["script_json"]["scenes"]
    assert len(scenes) == 4  # tasks[0].count
    for scene in scenes:
        assert scene["visual_direction"] == "未来感"


async def test_mapper_establishes_plan_trace(env) -> None:
    _factory, _scripts, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = ScriptMapper().map(plan)
    assert mapped["plan_id"] == plan.id
    assert mapped["script_json"]["plan_id"] == plan.id


async def test_service_create_script_from_plan(env) -> None:
    _factory, script_repository, plan_repository, service = env
    plan = await _make_plan(plan_repository)
    payload = await service.create_script_from_plan(plan.id)
    assert payload["plan_id"] == plan.id
    assert payload["topic"] == "人工智能科普短视频"
    script = await script_repository.get_script(payload["script_id"])
    assert script is not None and script.platform == "tiktok"


async def test_service_rejects_non_short_video_intent(env) -> None:
    _factory, _scripts, plan_repository, service = env
    plan = await plan_repository.create_plan(
        request_text="手工皮具钱包",
        intent="product_ad",
        plan_json={"summary": "钱包主图", "target_audience": "女性"},
    )
    with pytest.raises(ValueError, match="not short_video"):
        await service.create_script_from_plan(plan.id)


async def test_service_plan_trace_back(env) -> None:
    _factory, _scripts, plan_repository, service = env
    plan = await _make_plan(plan_repository)
    script_payload = await service.create_script_from_plan(plan.id)
    traced = await service.get_plan_for_script(script_payload["script_id"])
    assert traced is not None
    assert traced["plan_id"] == plan.id
    assert traced["intent"] == "short_video"


@pytest.fixture
async def api_client(env):
    _factory, _scripts, _plans, service = env
    app = FastAPI()
    app.include_router(videos_router, prefix="/api")
    app.dependency_overrides[get_video_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _scripts, _plans, service


async def test_api_from_plan_and_trace(api_client) -> None:
    client, _factory, _scripts, plan_repository, _service = api_client
    plan = await _make_plan(plan_repository)
    created = await client.post(f"/api/videos/scripts/from-plan/{plan.id}")
    assert created.status_code == 201
    body = created.json()
    assert body["plan_id"] == plan.id
    assert body["topic"] == "人工智能科普短视频"

    traced = await client.get(f"/api/videos/scripts/{body['script_id']}/plan")
    assert traced.status_code == 200
    assert traced.json()["intent"] == "short_video"
