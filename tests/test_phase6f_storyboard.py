"""Phase 06-F B2 storyboard planner injection tests."""

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
from pixelle_video.orchestration.agents.sub_agents import StoryboardPlanner
from pixelle_video.videos.repository import VideoScriptRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'b2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    from pixelle_video.media_jobs.repository import MediaJobRepository

    service = VideoApplicationService(repository, job_repository=MediaJobRepository(factory))
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _make_script(repository: VideoScriptRepository, **extra):
    return await repository.create_script(
        topic="AI 科普",
        target_duration=30,
        platform="tiktok",
        language="zh-CN",
        script_json={
            "hook": "大模型是什么",
            "scenes": [{"index": 1, "visual_direction": "未来感"}],
            "visual_style": {"mood": "未来感"},
        },
        **extra,
    )


async def _storyboard_llm(text: str) -> str:
    return (
        '{"scenes": [{"index": 1, "desc": "开场", "camera": "推近"},'
        ' {"index": 2, "desc": "主体", "camera": "跟拍"}],'
        ' "camera_notes": "低角度运镜"}'
    )


async def test_storyboard_uses_04e_planner_when_injected(env) -> None:
    _factory, repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    script = await _make_script(repository)
    payload = await service.build_storyboard(script.id)
    assert payload["source"] == "04-e-storyboard-planner"
    assert len(payload["frames"]) == 2
    assert payload["frames"][0]["camera"] == "推近"
    assert payload["camera_notes"] == "低角度运镜"


async def test_storyboard_agent_uses_visual_style(env) -> None:
    _factory, repository, service = env
    captured: list[str] = []

    async def spy_llm(text: str) -> str:
        captured.append(text)
        return await _storyboard_llm(text)

    service.storyboard_planner = StoryboardPlanner(spy_llm)
    script = await _make_script(repository)
    await service.build_storyboard(script.id)
    assert "未来感" in captured[0]
    assert "AI 科普" in captured[0]


async def test_storyboard_agent_persists_storyboard_json(env) -> None:
    _factory, repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    script = await _make_script(repository)
    await service.build_storyboard(script.id)
    stored = await repository.get_script(script.id)
    assert stored.status == "storyboarding"
    storyboard = (stored.script_json or {}).get("storyboard")
    assert storyboard is not None
    assert len(storyboard["frames"]) == 2
    assert storyboard["frames"][0]["job_id"] is None
    assert storyboard["frames"][0]["image_prompt"]


async def test_storyboard_falls_back_to_engine(env) -> None:
    """Backwards compatibility: without the planner agent, the engine path
    still returns a storyboard."""
    _factory, repository, service = env
    script = await _make_script(repository)
    payload = await service.build_storyboard(script.id)
    assert "script_id" in payload
    assert "frames" in payload or "storyboard" in payload


async def test_storyboard_agent_passes_04a_compiler(env) -> None:
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm, prompt_compiler=prompt_compile)
    script = await _make_script(repository)
    payload = await service.build_storyboard(script.id)
    assert payload["source"] == "04-e-storyboard-planner"
    assert len(payload["frames"]) == 2


async def test_storyboard_missing_script_raises(env) -> None:
    _factory, _repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    from pixelle_video.videos.repository import VideoScriptNotFoundError

    with pytest.raises(VideoScriptNotFoundError):
        await service.build_storyboard("missing")


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(videos_router, prefix="/api")
    app.dependency_overrides[get_video_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_storyboard_with_agent(api_client) -> None:
    client, _factory, repository, service = api_client
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    script = await _make_script(repository)
    response = await client.post(f"/api/videos/scripts/{script.id}/storyboard")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "04-e-storyboard-planner"
    assert len(body["frames"]) == 2


async def test_api_storyboard_legacy_still_works(api_client) -> None:
    client, _factory, repository, _service = api_client
    script = await _make_script(repository)
    response = await client.post(f"/api/videos/scripts/{script.id}/storyboard")
    assert response.status_code == 200
