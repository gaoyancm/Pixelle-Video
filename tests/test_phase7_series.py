"""Phase 07 C2 anime series structure tests."""

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


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'c2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    service = AnimeApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _seed_series(repository: AnimeRepository) -> tuple[str, str, str]:
    project = await repository.create_anime_project(
        project_id="project-x", world_setting="仙侠世界", style_profile="2D"
    )
    episode = await repository.create_episode(
        anime_project_id=project.id, season_no=1, episode_no=1, title="蜀山初遇"
    )
    scene = await repository.create_scene_in_episode(
        episode_id=episode.id,
        scene_no=1,
        description="大殿对峙",
        characters=[{"character_id": "char-1", "status": "登场"}],
    )
    return project.id, episode.id, scene.id


async def test_create_anime_project(env) -> None:
    _factory, repository, _service = env
    project = await repository.create_anime_project(
        project_id="project-x", world_setting="仙侠世界", style_profile="水墨"
    )
    fetched = await repository.get_anime_project(project.id)
    assert fetched is not None and fetched.style_profile == "水墨"


async def test_create_episode_unique_season_episode(env) -> None:
    _factory, repository, _service = env
    project = await repository.create_anime_project(project_id="project-x")
    await repository.create_episode(anime_project_id=project.id, season_no=1, episode_no=1)
    with pytest.raises(Exception):
        await repository.create_episode(anime_project_id=project.id, season_no=1, episode_no=1)


async def test_create_scene_in_episode(env) -> None:
    _factory, repository, _service = env
    _project, episode_id, _scene = await _seed_series(repository)
    scene = await repository.get_scene_in_episode(_scene)
    assert scene is not None and scene.characters_json[0]["character_id"] == "char-1"
    assert scene.status == "draft"


async def test_create_shot_with_parent_reference(env) -> None:
    _factory, repository, _service = env
    _project, _episode, scene_id = await _seed_series(repository)
    parent = await repository.create_shot(
        scene_id=scene_id,
        shot_no=1,
        duration_sec=5,
        visual_description="全景",
        camera_setup={"angle": "低角度"},
    )
    child = await repository.create_shot(
        scene_id=scene_id,
        shot_no=2,
        duration_sec=4,
        visual_description="特写",
        parent_shot_id=parent.id,
    )
    fetched_child = await repository.get_shot(child.id)
    assert fetched_child is not None
    assert fetched_child.parent_shot_id == parent.id
    assert fetched_child.camera_setup_json is None  # child inherits nothing yet


async def test_list_shots_ordered(env) -> None:
    _factory, repository, _service = env
    _project, _episode, scene_id = await _seed_series(repository)
    await repository.create_shot(
        scene_id=scene_id, shot_no=2, duration_sec=4, visual_description="特写"
    )
    await repository.create_shot(
        scene_id=scene_id, shot_no=1, duration_sec=5, visual_description="全景"
    )
    shots = await repository.list_shots(scene_id)
    assert [shot.shot_no for shot in shots] == [1, 2]


async def test_update_shot_status_and_asset(env) -> None:
    _factory, repository, _service = env
    _project, _episode, scene_id = await _seed_series(repository)
    shot = await repository.create_shot(
        scene_id=scene_id, shot_no=1, duration_sec=5, visual_description="全景"
    )
    updated = await repository.update_shot(
        shot.id, status="succeeded", generated_asset_id="asset-v1"
    )
    assert updated.status == "succeeded"
    assert updated.generated_asset_id == "asset-v1"


async def test_service_create_series_chain(env) -> None:
    _factory, repository, service = env
    from api.schemas.anime import (
        AnimeProjectCreate,
        EpisodeCreate,
        SceneInEpisodeCreate,
        ShotCreate,
    )

    project = await service.create_anime_project(
        AnimeProjectCreate(project_id="project-x", world_setting="仙侠")
    )
    episode = await service.create_episode(
        EpisodeCreate(anime_project_id=project.id, season_no=1, episode_no=2)
    )
    scene = await service.create_scene_in_episode(
        SceneInEpisodeCreate(episode_id=episode.id, scene_no=1, characters=[{"character_id": "c1"}])
    )
    shot = await service.create_shot(
        ShotCreate(
            scene_id=scene.id,
            shot_no=1,
            duration_sec=5,
            visual_description="推镜头",
            camera_setup={"movement": "跟拍"},
        )
    )
    assert shot.parent_shot_id is None
    assert shot.shot_no == 1


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


async def test_api_four_layer_crud(api_client) -> None:
    client, _factory, repository, _service = api_client
    project = await client.post(
        "/api/anime/projects",
        json={"project_id": "project-x", "world_setting": "仙侠"},
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    episode = await client.post(
        "/api/anime/episodes",
        json={"anime_project_id": project_id, "season_no": 1, "episode_no": 1},
    )
    assert episode.status_code == 201
    episode_id = episode.json()["id"]

    scene = await client.post(
        f"/api/anime/episodes/{episode_id}/scenes",
        json={"episode_id": episode_id, "scene_no": 1, "characters": [{"character_id": "c1"}]},
    )
    assert scene.status_code == 201
    scene_id = scene.json()["id"]

    shot = await client.post(
        f"/api/anime/scenes/{scene_id}/shots",
        json={
            "scene_id": scene_id,
            "shot_no": 1,
            "duration_sec": 5,
            "visual_description": "开篇全景",
            "camera_setup": {"angle": "低角度"},
        },
    )
    assert shot.status_code == 201
    assert shot.json()["shot_no"] == 1
    assert shot.json()["parent_shot_id"] is None


async def test_api_shot_parent_reference(api_client) -> None:
    client, _factory, repository, _service = api_client
    _project, _episode, scene_id = await _seed_series(repository)
    parent = await client.post(
        f"/api/anime/scenes/{scene_id}/shots",
        json={"scene_id": scene_id, "shot_no": 1, "duration_sec": 5, "visual_description": "全景"},
    )
    child = await client.post(
        f"/api/anime/scenes/{scene_id}/shots",
        json={
            "scene_id": scene_id,
            "shot_no": 2,
            "duration_sec": 4,
            "visual_description": "特写",
            "parent_shot_id": parent.json()["id"],
        },
    )
    assert child.json()["parent_shot_id"] == parent.json()["id"]


async def test_list_scenes_in_episode_ordered(env) -> None:
    _factory, repository, _service = env
    _project, episode_id, _scene = await _seed_series(repository)
    await repository.create_scene_in_episode(
        episode_id=episode_id, scene_no=2, description="第二场"
    )
    scenes = await repository.list_scenes_in_episode(episode_id)
    assert [scene.scene_no for scene in scenes] == [1, 2]
