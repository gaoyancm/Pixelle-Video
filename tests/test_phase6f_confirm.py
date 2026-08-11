"""Phase 06-F B3 confirm-loop tests."""

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
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.orchestration.agents.sub_agents import StoryboardPlanner
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_mapper import ScriptMapper
from tests.test_phase6f_storyboard import _make_script, _storyboard_llm


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'b3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    script_repository = VideoScriptRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    service = VideoApplicationService(
        script_repository,
        job_repository=MediaJobRepository(factory),
        storyboard_planner=StoryboardPlanner(_storyboard_llm),
        plan_repository=plan_repository,
        script_mapper=ScriptMapper(),
    )
    try:
        yield factory, script_repository, plan_repository, service
    finally:
        await engine.dispose()


async def test_confirm_from_plan_runs_storyboard_agent(env) -> None:
    _factory, script_repository, _plans, service = env
    script = await _make_script(script_repository)
    payload = await service.confirm_from_plan(script.id)
    assert payload["source"] == "04-e-storyboard-planner"
    assert len(payload["frames"]) == 2
    updated = await script_repository.get_script(script.id)
    assert updated.status == "composing"


async def test_confirm_from_plan_uses_engine_without_agent(env) -> None:
    _factory, script_repository, _plans, service = env
    service.storyboard_planner = None
    script = await _make_script(script_repository)
    payload = await service.confirm_from_plan(script.id)
    assert payload["source"] == "engine"
    updated = await script_repository.get_script(script.id)
    assert updated.status == "composing"


async def test_confirm_missing_script_raises(env) -> None:
    _factory, _scripts, _plans, service = env
    from pixelle_video.videos.repository import VideoScriptNotFoundError

    with pytest.raises(VideoScriptNotFoundError):
        await service.confirm_from_plan("missing")


async def test_full_plan_to_compose_chain(env) -> None:
    """B3 end-to-end: plan -> script -> confirm -> composing."""
    _factory, script_repository, plan_repository, service = env
    plan = await plan_repository.create_plan(
        request_text="人工智能科普短视频",
        intent="short_video",
        plan_json={
            "summary": "AI 科普",
            "target_audience": "年轻人",
            "platforms": ["tiktok"],
            "visual_style": {"mood": "未来感"},
            "tasks": [{"type": "video", "count": 3}],
        },
    )
    script_payload = await service.create_script_from_plan(plan.id)
    assert script_payload["plan_id"] == plan.id
    confirmed = await service.confirm_from_plan(script_payload["script_id"])
    assert confirmed["source"] == "04-e-storyboard-planner"
    assert len(confirmed["frames"]) == 2
    stored = await script_repository.get_script(script_payload["script_id"])
    assert stored.status == "composing"
    assert (stored.script_json or {}).get("storyboard") is not None


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


async def test_api_confirm_from_plan(api_client) -> None:
    client, _factory, script_repository, _plans, _service = api_client
    script = await _make_script(script_repository)
    response = await client.post(f"/api/videos/scripts/{script.id}/confirm-from-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "04-e-storyboard-planner"
    assert len(body["frames"]) == 2


async def test_api_full_plan_to_confirm(api_client) -> None:
    client, _factory, _scripts, plan_repository, _service = api_client
    plan = await plan_repository.create_plan(
        request_text="AI 科普短视频",
        intent="short_video",
        plan_json={
            "summary": "AI 科普",
            "target_audience": "年轻人",
            "platforms": ["tiktok"],
            "visual_style": {"mood": "未来感"},
            "tasks": [{"type": "video", "count": 3}],
        },
    )
    created = await client.post(f"/api/videos/scripts/from-plan/{plan.id}")
    assert created.status_code == 201
    script_id = created.json()["script_id"]
    confirmed = await client.post(f"/api/videos/scripts/{script_id}/confirm-from-plan")
    assert confirmed.status_code == 200
    assert confirmed.json()["source"] == "04-e-storyboard-planner"
