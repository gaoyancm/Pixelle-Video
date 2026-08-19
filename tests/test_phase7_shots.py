"""Phase 07 C3 shot production engine tests."""

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
from pixelle_video.anime.shot_engine import ShotProductionEngine
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'c3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    job_repository = MediaJobRepository(factory)
    engine_c3 = ShotProductionEngine(
        repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    service = AnimeApplicationService(
        repository, job_repository=job_repository, shot_engine=engine_c3
    )
    try:
        yield factory, repository, job_repository, service, engine_c3
    finally:
        await engine.dispose()


async def _seed_scene(repository: AnimeRepository) -> tuple[str, str]:
    project = await repository.create_anime_project(project_id="project-x")
    episode = await repository.create_episode(
        anime_project_id=project.id, season_no=1, episode_no=1
    )
    scene = await repository.create_scene_in_episode(
        episode_id=episode.id,
        scene_no=1,
        characters=[{"character_id": "char-1", "status": "登场"}],
    )
    return scene.id, episode.id


async def _seed_shots(repository: AnimeRepository, scene_id: str) -> list[str]:
    ids = []
    for index, duration in ((1, 5), (2, 4), (3, 6)):
        shot = await repository.create_shot(
            scene_id=scene_id,
            shot_no=index,
            duration_sec=duration,
            visual_description=f"镜头{index}",
            camera_setup={"angle": "低角度"} if index == 1 else None,
        )
        ids.append(shot.id)
    return ids


async def test_plan_shots_compiles_prompts(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    scene_id, _episode = await _seed_scene(repository)
    shot_ids = await _seed_shots(repository, scene_id)
    compiled = await engine.plan_shots(scene_id)
    assert len(compiled) == 3
    for item in compiled:
        assert item["image_prompt"] and item["video_prompt"]
    shot = await repository.get_shot(shot_ids[0])
    assert shot.image_prompt and shot.video_prompt


async def test_generate_shot_creates_job(env) -> None:
    _factory, repository, job_repository, _service, engine = env
    scene_id, _episode = await _seed_scene(repository)
    shot_ids = await _seed_shots(repository, scene_id)
    await engine.plan_shots(scene_id)
    payload = await engine.generate_shot(shot_ids[0])
    assert payload["status"] == "queued"
    assert (await job_repository.get_job(payload["job_id"])).node_id == "test-node"
    job = await job_repository.get_job(payload["job_id"])
    assert job is not None
    assert job.input_json["shot_id"] == shot_ids[0]
    assert job.input_json["prompt"] == job.input_json["video_prompt"]
    assert "prompt_hint" not in job.input_json
    shot = await repository.get_shot(shot_ids[0])
    assert shot.status == "queued"
    assert shot.generated_asset_id == payload["job_id"]


async def test_generate_scene_parents_first(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    scene_id, _episode = await _seed_scene(repository)
    shot_ids = await _seed_shots(repository, scene_id)
    # shot 2 is a child of shot 1.
    await repository.create_shot(
        scene_id=scene_id,
        shot_no=4,
        duration_sec=4,
        visual_description="子镜头",
        parent_shot_id=shot_ids[0],
    )
    await engine.plan_shots(scene_id)
    payload = await engine.generate_scene(scene_id)
    assert len(payload["shots"]) == 4
    parent_first = payload["shots"][0]
    assert parent_first["parent_shot_id"] is None


async def test_retry_shot_local_redo(env) -> None:
    _factory, repository, job_repository, _service, engine = env
    scene_id, _episode = await _seed_scene(repository)
    shot_ids = await _seed_shots(repository, scene_id)
    await engine.plan_shots(scene_id)
    first = await engine.generate_shot(shot_ids[0])
    retried = await engine.retry_shot(shot_ids[0])
    assert retried["job_id"] != first["job_id"]  # new job, only this shot
    shot = await repository.get_shot(shot_ids[0])
    assert shot.generated_asset_id == retried["job_id"]


async def test_progress_counts_states(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    scene_id, _episode = await _seed_scene(repository)
    shot_ids = await _seed_shots(repository, scene_id)
    await engine.plan_shots(scene_id)
    await engine.generate_shot(shot_ids[0])
    progress = await engine.progress(scene_id)
    assert progress["total"] == 3
    assert progress["states"]["queued"] == 1
    assert progress["states"]["pending"] == 2


async def test_plan_shots_missing_scene_raises(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    from pixelle_video.anime.repository import AnimeNotFoundError

    with pytest.raises(AnimeNotFoundError):
        await engine.plan_shots("missing")


async def test_missing_a800_reports_required_workflow_before_job_write(env) -> None:
    _factory, repository, job_repository, _service, _engine = env
    scene_id, _episode = await _seed_scene(repository)
    await _seed_shots(repository, scene_id)

    def no_a800(_workflow: str) -> str:
        raise RuntimeError("workflow unavailable")

    blocked = ShotProductionEngine(repository, job_repository, node_selector=no_a800)
    blocked_service = AnimeApplicationService(
        repository, job_repository=job_repository, shot_engine=blocked
    )
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: blocked_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/anime/scenes/{scene_id}/generate")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "required_workflow_unavailable",
        "message": "Anime generation requires unavailable workflow 'a800_wan22_t2v_33f'.",
        "required_workflow": "a800_wan22_t2v_33f",
    }
    assert await job_repository.list_jobs() == []


@pytest.fixture
async def api_client(env):
    _factory, _repository, _jobs, service, _engine = env
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_plan_generate_progress(api_client) -> None:
    client, _factory, repository, _service = api_client
    scene_id, _episode = await _seed_scene(repository)
    await _seed_shots(repository, scene_id)
    plan = await client.post(f"/api/anime/scenes/{scene_id}/plan")
    assert plan.status_code == 200
    assert len(plan.json()["shots"]) == 3

    generated = await client.post(f"/api/anime/scenes/{scene_id}/generate")
    assert generated.status_code == 200
    assert len(generated.json()["shots"]) == 3

    progress = await client.get(f"/api/anime/scenes/{scene_id}/progress")
    assert progress.status_code == 200
    assert progress.json()["total"] == 3


async def test_api_retry_shot(api_client) -> None:
    client, _factory, repository, _service = api_client
    scene_id, _episode = await _seed_scene(repository)
    shot_ids = await _seed_shots(repository, scene_id)
    await _service.plan_shots(scene_id)
    retried = await client.post(f"/api/anime/shots/{shot_ids[1]}/retry")
    assert retried.status_code == 200
    assert retried.json()["shot_id"] == shot_ids[1]
    assert retried.json()["status"] == "queued"


async def test_plan_shots_uses_injected_04a_compiler(env) -> None:
    """C3: prompts compile through the injected 04-A compiler ({{var}})."""
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, _jobs, _service, _engine = env
    engine = ShotProductionEngine(
        repository,
        _jobs,
        prompt_compiler=prompt_compile,
        image_prompt_builder=lambda shot, ctx: "画面：{{shot}} 角色：{{characters}}",
    )
    scene_id, _episode = await _seed_scene(repository)
    await _seed_shots(repository, scene_id)
    compiled = await engine.plan_shots(scene_id)
    assert "{{shot}}" not in compiled[0]["image_prompt"]
    assert "镜头1" in compiled[0]["image_prompt"]


async def test_plan_shots_prompts_include_character_block(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    scene_id, _episode = await _seed_scene(repository)
    await repository.create_shot(
        scene_id=scene_id,
        shot_no=1,
        duration_sec=5,
        visual_description="对峙镜头",
        character_states=[{"char_id": "char-1", "emotion": "愤怒"}],
    )
    compiled = await engine.plan_shots(scene_id)
    assert "char-1" in compiled[0]["video_prompt"]
