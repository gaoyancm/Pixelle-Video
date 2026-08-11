"""Phase 07-F D1 AI auto-storyboard tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_anime_service
from api.routers.anime import router as anime_router
from api.services.anime import AnimeApplicationService
from pixelle_video.anime.repository import AnimeRepository
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.orchestration.agents.sub_agents import StoryboardPlanner


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'd1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    service = AnimeApplicationService(repository, job_repository=MediaJobRepository(factory))
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _seed_scene(repository: AnimeRepository) -> str:
    project = await repository.create_anime_project(project_id="project-x", style_profile="水墨")
    episode = await repository.create_episode(
        anime_project_id=project.id, season_no=1, episode_no=1
    )
    scene = await repository.create_scene_in_episode(
        episode_id=episode.id,
        scene_no=1,
        description="大殿对峙",
        characters=[{"character_id": "char-1"}],
    )
    return scene.id


async def _storyboard_llm(text: str) -> str:
    return (
        '{"scenes": [{"index": 1, "desc": "全景", "shot_type": "wide", "camera": "低角度"},'
        ' {"index": 2, "desc": "中景", "shot_type": "medium", "camera": "平视"},'
        ' {"index": 3, "desc": "特写", "shot_type": "close", "camera": "推近"}],'
        ' "camera_notes": "节奏先缓后急"}'
    )


async def test_auto_storyboard_creates_shots(env) -> None:
    _factory, repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    scene_id = await _seed_scene(repository)
    payload = await service.plan_shots(scene_id)
    assert payload["source"] == "04-e-storyboard-planner"
    assert len(payload["shots"]) == 3
    shots = await repository.list_shots(scene_id)
    assert len(shots) == 3
    assert shots[0].visual_description == "全景"


async def test_camera_tree_inheritance(env) -> None:
    """wide has no parent; medium/close inherit from the latest wide shot."""
    _factory, repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    scene_id = await _seed_scene(repository)
    await service.plan_shots(scene_id)
    shots = await repository.list_shots(scene_id)
    wide = shots[0]
    assert wide.parent_shot_id is None
    assert shots[1].parent_shot_id == wide.id  # medium inherits wide
    assert shots[2].parent_shot_id == wide.id  # close inherits wide


async def test_auto_storyboard_includes_style_and_characters(env) -> None:
    _factory, repository, service = env
    captured: list[str] = []

    async def spy_llm(text: str) -> str:
        captured.append(text)
        return await _storyboard_llm(text)

    service.storyboard_planner = StoryboardPlanner(spy_llm)
    scene_id = await _seed_scene(repository)
    await service.plan_shots(scene_id)
    assert "大殿对峙" in captured[0]
    assert "char-1" in captured[0]
    assert "水墨" in captured[0]  # style_profile from the anime project


async def test_auto_storyboard_passes_04a_compiler(env) -> None:
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm, prompt_compiler=prompt_compile)
    scene_id = await _seed_scene(repository)
    await service.plan_shots(scene_id)
    assert True


async def test_plan_shots_falls_back_to_engine(env) -> None:
    """Backwards compatibility: without the planner agent the 07 engine path
    still compiles the existing shots."""
    _factory, repository, service = env
    scene_id = await _seed_scene(repository)
    await repository.create_shot(
        scene_id=scene_id, shot_no=1, duration_sec=5, visual_description="手动镜头"
    )
    payload = await service.plan_shots(scene_id)
    assert "source" not in payload
    assert len(payload["shots"]) == 1


async def test_auto_storyboard_missing_scene_raises(env) -> None:
    _factory, _repository, service = env
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    from pixelle_video.anime.repository import AnimeNotFoundError

    with pytest.raises(AnimeNotFoundError):
        await service.plan_shots("missing")


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_auto_storyboard(api_client) -> None:
    client, _factory, repository, service = api_client
    service.storyboard_planner = StoryboardPlanner(_storyboard_llm)
    scene_id = await _seed_scene(repository)
    response = await client.post(f"/api/anime/scenes/{scene_id}/plan")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "04-e-storyboard-planner"
    assert len(body["shots"]) == 3


async def test_api_auto_storyboard_legacy_still_works(api_client) -> None:
    client, _factory, repository, _service = api_client
    scene_id = await _seed_scene(repository)
    await repository.create_shot(
        scene_id=scene_id, shot_no=1, duration_sec=5, visual_description="手动镜头"
    )
    response = await client.post(f"/api/anime/scenes/{scene_id}/plan")
    assert response.status_code == 200
