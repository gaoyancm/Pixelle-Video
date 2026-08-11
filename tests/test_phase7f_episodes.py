"""Phase 07-F D2 multi-episode planning tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_orchestration_service
from api.routers.orchestration import router as orchestration_router
from api.services.orchestration import OrchestrationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.agents.sub_agents import EpisodePlanner
from pixelle_video.orchestration.repository import ContentPlanRepository


async def _episode_llm(text: str) -> str:
    return (
        '{"seasons": [{"season_no": 1, "episodes": [{"episode_no": 1, "title": "第一集",'
        ' "hook": "悬念开场", "arc": "相识"}, {"episode_no": 2, "title": "第二集",'
        ' "hook": "危机", "arc": "冲突"}]}],'
        ' "character_arcs": [{"character_id": "c1", "season_arc": "从狂妄到谦卑",'
        ' "key_episodes": [3, 5, 8]}],'
        ' "foreshadowing_map": [{"setup": "发现钥匙", "payoff": "打开密室"}]}'
    )


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'd2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ContentPlanRepository(factory)
    service = OrchestrationService(repository, episode_planner=EpisodePlanner(_episode_llm))
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _make_animation_plan(repository: ContentPlanRepository):
    return await repository.create_plan(
        request_text="做一部仙侠长篇动画剧集",
        intent="animation",
        plan_json={
            "intent": "animation",
            "summary": "仙侠世界，主角从凡人到仙尊",
            "target_audience": "年轻观众",
            "platforms": ["bilibili"],
        },
    )


async def test_generate_episode_plan_extends_plan_json(env) -> None:
    _factory, repository, service = env
    plan = await _make_animation_plan(repository)
    payload = await service.generate_episode_plan(plan.id)
    assert len(payload["seasons"]) == 1
    seasons = payload["seasons"][0]
    assert seasons["season_no"] == 1
    assert len(seasons["episodes"]) == 2
    assert payload["character_arcs"][0]["season_arc"] == "从狂妄到谦卑"
    assert payload["foreshadowing_map"][0]["setup"] == "发现钥匙"


async def test_episode_plan_persisted(env) -> None:
    _factory, repository, service = env
    plan = await _make_animation_plan(repository)
    await service.generate_episode_plan(plan.id)
    stored = await repository.get_plan(plan.id)
    assert stored.plan_json["seasons"][0]["episodes"][1]["arc"] == "冲突"
    assert stored.plan_json["character_arcs"][0]["key_episodes"] == [3, 5, 8]


async def test_generate_episode_plan_rejects_non_animation(env) -> None:
    _factory, repository, service = env
    plan = await repository.create_plan(
        request_text="手工皮具钱包",
        intent="product_ad",
        plan_json={"summary": "钱包"},
    )
    with pytest.raises(ValueError, match="not animation"):
        await service.generate_episode_plan(plan.id)


async def test_episode_plan_hooks_present(env) -> None:
    _factory, repository, service = env
    plan = await _make_animation_plan(repository)
    payload = await service.generate_episode_plan(plan.id)
    hooks = [episode["hook"] for season in payload["seasons"] for episode in season["episodes"]]
    assert "悬念开场" in hooks
    assert "危机" in hooks


async def test_episode_plan_with_04a_compiler(env) -> None:
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, service = env
    service.episode_planner = EpisodePlanner(_episode_llm, prompt_compiler=prompt_compile)
    plan = await _make_animation_plan(repository)
    payload = await service.generate_episode_plan(plan.id)
    assert len(payload["seasons"]) == 1


async def test_episode_plan_without_planner_raises(env) -> None:
    _factory, repository, service = env
    service.episode_planner = None
    plan = await _make_animation_plan(repository)
    with pytest.raises(RuntimeError, match="episode planner"):
        await service.generate_episode_plan(plan.id)


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(orchestration_router, prefix="/api")
    app.dependency_overrides[get_orchestration_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_generate_episode_plan(api_client) -> None:
    client, _factory, repository, _service = api_client
    plan = await _make_animation_plan(repository)
    response = await client.post(f"/api/orchestration/plans/{plan.id}/generate-episode-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["plan_id"] == plan.id
    assert len(body["seasons"]) == 1
    assert body["character_arcs"][0]["character_id"] == "c1"


async def test_episode_plan_foreshadowing_mapping(env) -> None:
    _factory, repository, service = env
    plan = await _make_animation_plan(repository)
    payload = await service.generate_episode_plan(plan.id)
    foreshadowing = payload["foreshadowing_map"]
    assert len(foreshadowing) == 1
    assert foreshadowing[0]["payoff"] == "打开密室"
