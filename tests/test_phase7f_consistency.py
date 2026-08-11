"""Phase 07-F D3 cross-episode consistency tests."""

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
from pixelle_video.orchestration.agents.sub_agents import ConsistencyVerifier
from pixelle_video.orchestration.repository import ContentPlanRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'd3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    service = AnimeApplicationService(repository, plan_repository=plan_repository)
    try:
        yield factory, repository, plan_repository, service
    finally:
        await engine.dispose()


async def _make_character(repository: AnimeRepository):
    return await repository.create_character(
        name="李逍遥",
        description="蜀山弟子",
        identity_anchors={"hair": {"hair_style": "束发"}},
        static_features={"gender": "男"},
    )


async def _seed_shots(repository: AnimeRepository, character_id: str) -> list[str]:
    ids = []
    for scene_no in (1, 2):
        scene = await repository.create_scene_in_episode(
            episode_id=None,
            scene_no=scene_no,
            characters=[{"character_id": character_id}],
        )
        shot = await repository.create_shot(
            scene_id=scene.id,
            shot_no=1,
            duration_sec=5,
            visual_description=f"第{scene_no}集镜头",
            character_states=[{"char_id": character_id}],
        )
        await repository.update_shot(
            shot.id, status="succeeded", generated_asset_id=f"asset-{scene_no}"
        )
        ids.append(shot.id)
    return ids


async def _arc_llm(text: str) -> str:
    return '{"consistent": true, "issues": []}'


async def test_cross_episode_consistency_passes(env) -> None:
    _factory, repository, plan_repository, service = env
    service.consistency_verifier = ConsistencyVerifier(_arc_llm)
    character = await _make_character(repository)
    await _seed_shots(repository, character.id)
    report = await service.cross_episode_consistency(character.id, 1)
    assert report["character_name"] == "李逍遥"
    assert report["shots_scanned"] == 2
    assert report["consistent"] is True


async def test_cross_episode_consistency_reads_arc_from_plan(env) -> None:
    _factory, repository, plan_repository, service = env
    captured: list[str] = []

    async def spy_llm(text: str) -> str:
        captured.append(text)
        return await _arc_llm(text)

    service.consistency_verifier = ConsistencyVerifier(spy_llm)
    character = await _make_character(repository)
    await plan_repository.create_plan(
        request_text="动画剧集",
        intent="animation",
        plan_json={
            "summary": "仙侠",
            "character_arcs": [
                {"character_id": character.id, "season_arc": "从狂妄到谦卑", "key_episodes": [3, 5]}
            ],
        },
    )
    await _seed_shots(repository, character.id)
    report = await service.cross_episode_consistency(character.id, 1)
    assert report["arc"] == "从狂妄到谦卑"
    assert "从狂妄到谦卑" in captured[0]


async def test_cross_episode_consistency_no_shots(env) -> None:
    _factory, repository, _plans, service = env
    service.consistency_verifier = ConsistencyVerifier(_arc_llm)
    character = await _make_character(repository)
    report = await service.cross_episode_consistency(character.id, 1)
    assert report["shots_scanned"] == 0
    assert report["consistent"] is True  # planner skipped without shots


async def test_cross_episode_consistency_missing_character_raises(env) -> None:
    _factory, _repository, _plans, service = env
    from pixelle_video.anime.repository import AnimeNotFoundError

    with pytest.raises(AnimeNotFoundError):
        await service.cross_episode_consistency("missing", 1)


async def test_cross_episode_consistency_flags_issues(env) -> None:
    _factory, repository, _plans, service = env

    async def failing_llm(text: str) -> str:
        return '{"consistent": false, "issues": ["服装与剧情弧线不符"]}'

    service.consistency_verifier = ConsistencyVerifier(failing_llm)
    character = await _make_character(repository)
    await _seed_shots(repository, character.id)
    report = await service.cross_episode_consistency(character.id, 1)
    assert report["consistent"] is False
    assert report["issues"] == ["服装与剧情弧线不符"]


async def test_cross_episode_consistency_passes_04a_compiler(env) -> None:
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, _plans, service = env
    service.consistency_verifier = ConsistencyVerifier(_arc_llm, prompt_compiler=prompt_compile)
    character = await _make_character(repository)
    await _seed_shots(repository, character.id)
    report = await service.cross_episode_consistency(character.id, 1)
    assert report["shots_scanned"] == 2


@pytest.fixture
async def api_client(env):
    _factory, _repository, _plans, service = env
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, _plans, service


async def test_api_cross_episode_consistency(api_client) -> None:
    client, _factory, repository, _plans, service = api_client
    service.consistency_verifier = ConsistencyVerifier(_arc_llm)
    character = await _make_character(repository)
    await _seed_shots(repository, character.id)
    response = await client.get(f"/api/anime/characters/{character.id}/cross-episode-consistency/1")
    assert response.status_code == 200
    body = response.json()
    assert body["shots_scanned"] == 2
    assert body["season_no"] == 1
