"""Phase 07 C4 cross-shot consistency guard tests."""

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
from pixelle_video.anime.consistency import ConsistencyGuard
from pixelle_video.anime.repository import AnimeRepository
from pixelle_video.media_jobs.models import Base


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'c4.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    guard = ConsistencyGuard(repository)
    service = AnimeApplicationService(repository, consistency_guard=guard)
    try:
        yield factory, repository, guard, service
    finally:
        await engine.dispose()


ANCHORS = {
    "bone_structure": {"face_shape": "oval", "jawline": "defined"},
    "facial_features": {"eye_shape": "almond"},
    "unique_marks": ["左眼下小痣"],
    "color_palette": {"hair": "#1A1A1A", "skin": "#E8C4A0"},
    "texture": {"skin_texture": "smooth"},
    "hair": {"hair_style": "束发高髻"},
}


async def _make_character(repository: AnimeRepository):
    return await repository.create_character(
        name="李逍遥",
        description="蜀山弟子",
        identity_anchors=ANCHORS,
        static_features={"gender": "男", "height": "175", "face_shape": "oval"},
        dynamic_features={"costume": "蓝色道袍"},
        reference_images=[{"view": "front", "asset_id": "ref-1"}],
    )


async def test_build_shot_prompt_injects_six_layer_anchors(env) -> None:
    _factory, repository, guard, _service = env
    character = await _make_character(repository)
    shot = await repository.create_shot(
        scene_id="scene-1", shot_no=1, duration_sec=5, visual_description="特写镜头"
    )
    prompt = await guard.build_shot_prompt(shot, character)
    assert "李逍遥" in prompt
    assert "face_shape:oval" in prompt
    assert "束发高髻" in prompt
    assert "gender:男" in prompt  # static feature injected


async def test_build_shot_prompt_static_dynamic_separation(env) -> None:
    _factory, repository, guard, _service = env
    character = await _make_character(repository)
    shot = await repository.create_shot(
        scene_id="scene-1", shot_no=1, duration_sec=5, visual_description="镜头"
    )
    prompt = await guard.build_shot_prompt(shot, character)
    assert "costume" not in prompt  # dynamic features not in static prompt
    assert "height:175" in prompt  # static feature present


async def test_select_reference_images_most_recent_first(env) -> None:
    _factory, repository, guard, _service = env
    character = await _make_character(repository)
    first = await _shot(repository, 1, "pending", None)
    succeeded = await repository.create_shot(
        scene_id="scene-1", shot_no=2, duration_sec=5, visual_description="镜头2"
    )
    succeeded = await repository.update_shot(
        succeeded.id, status="succeeded", generated_asset_id="asset-2"
    )
    refs = await guard.select_reference_images(character, [first, succeeded])
    assert refs == ["asset-2"]  # only the succeeded shot qualifies


async def test_select_reference_images_falls_back_to_registered_refs(env) -> None:
    _factory, repository, guard, _service = env
    character = await _make_character(repository)
    refs = await guard.select_reference_images(character, [])
    assert refs == ["ref-1"]  # fallback to character reference images


async def test_check_consistency_passes_on_clean_qc(env) -> None:
    _factory, repository, guard, _service = env
    shot = await repository.create_shot(
        scene_id="scene-1", shot_no=1, duration_sec=5, visual_description="镜头"
    )
    report = await guard.check_consistency(shot, {"issues": []})
    assert report["consistent"] is True


async def test_check_consistency_flags_character_issues(env) -> None:
    _factory, repository, guard, _service = env
    shot = await repository.create_shot(
        scene_id="scene-1", shot_no=1, duration_sec=5, visual_description="镜头"
    )
    report = await guard.check_consistency(
        shot, {"issues": [{"category": "character", "message": "脸型不一致"}]}
    )
    assert report["consistent"] is False
    assert len(report["issues"]) == 1


async def test_check_consistency_ignores_unrelated_categories(env) -> None:
    _factory, repository, guard, _service = env
    shot = await repository.create_shot(
        scene_id="scene-1", shot_no=1, duration_sec=5, visual_description="镜头"
    )
    report = await guard.check_consistency(
        shot, {"issues": [{"category": "technical", "message": "分辨率低"}]}
    )
    assert report["consistent"] is True


async def test_character_report(env) -> None:
    _factory, repository, guard, _service = env
    character = await _make_character(repository)
    report = await guard.character_report(character.id)
    assert report["character_name"] == "李逍遥"
    assert report["static_features"]["gender"] == "男"
    assert report["dynamic_features"]["costume"] == "蓝色道袍"


async def test_episode_report_counts_shots(env) -> None:
    _factory, repository, guard, _service = env
    project = await repository.create_anime_project(project_id="project-x")
    episode = await repository.create_episode(
        anime_project_id=project.id, season_no=1, episode_no=1
    )
    scene = await repository.create_scene_in_episode(episode_id=episode.id, scene_no=1)
    await repository.create_shot(
        scene_id=scene.id, shot_no=1, duration_sec=5, visual_description="镜头1"
    )
    report = await guard.episode_report(episode.id)
    assert report["scenes"] == 1
    assert report["shot_counts"]["pending"] == 1
    assert report["consistent"] is True


@pytest.fixture
async def api_client(env):
    _factory, _repository, _guard, service = env
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_character_consistency_report(api_client) -> None:
    client, _factory, repository, _service = api_client
    character = await _make_character(repository)
    report = await client.get(f"/api/anime/characters/{character.id}/consistency-report")
    assert report.status_code == 200
    assert report.json()["character_name"] == "李逍遥"


async def test_api_episode_consistency_report(api_client) -> None:
    client, _factory, repository, _service = api_client
    project = await repository.create_anime_project(project_id="project-x")
    episode = await repository.create_episode(
        anime_project_id=project.id, season_no=1, episode_no=1
    )
    scene = await repository.create_scene_in_episode(episode_id=episode.id, scene_no=1)
    await repository.create_shot(
        scene_id=scene.id, shot_no=1, duration_sec=5, visual_description="镜头1"
    )
    report = await client.get(f"/api/anime/episodes/{episode.id}/consistency-report")
    assert report.status_code == 200
    assert report.json()["scenes"] == 1


async def _shot(repository: AnimeRepository, shot_no: int, status: str, asset_id):
    shot = await repository.create_shot(
        scene_id="scene-1",
        shot_no=shot_no,
        duration_sec=5,
        visual_description=f"镜头{shot_no}",
    )
    return shot
