"""Phase 04-E L1 intent routing and content plan tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_orchestration_service
from api.routers.orchestration import router as orchestration_router
from api.services.orchestration import OrchestrationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.orchestration.router import IntentRouter


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'l1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ContentPlanRepository(factory)
    router = IntentRouter()
    service = OrchestrationService(repository, intent_router=router)
    try:
        yield factory, repository, service, router
    finally:
        await engine.dispose()


async def test_classify_product_ad(env) -> None:
    _factory, _repository, _service, router = env
    assert await router.classify("手工皮具钱包，Etsy主图+TikTok视频") == "product_ad"
    assert await router.classify("生成一张产品主图") == "product_ad"


async def test_classify_short_video(env) -> None:
    _factory, _repository, _service, router = env
    assert await router.classify("做一个人工智能科普短视频") == "short_video"
    assert await router.classify("写一个口播脚本") == "short_video"


async def test_classify_animation(env) -> None:
    _factory, _repository, _service, router = env
    assert await router.classify("做一部仙侠长篇动画剧集") == "animation"
    assert await router.classify("设计一个动漫角色") == "animation"


async def test_classify_unknown(env) -> None:
    _factory, _repository, _service, router = env
    assert await router.classify("今天天气不错") == "unknown"


async def test_classify_with_knowledge_boost(env) -> None:
    _factory, _repository, _service, _router = env
    calls: list[str] = []

    async def fake_knowledge(text: str) -> list[dict]:
        calls.append(text)
        return [{"content": "品牌规范"}]

    router = IntentRouter(knowledge_querier=fake_knowledge)
    assert await router.classify("手工皮具钱包主图") == "product_ad"
    assert calls == ["手工皮具钱包主图"]


async def test_build_initial_plan_skeleton(env) -> None:
    _factory, _repository, _service, router = env
    plan = router.build_initial_plan("需求", "product_ad")
    assert plan["intent"] == "product_ad"
    assert plan["summary"] == "需求"
    assert "tasks" in plan and "creative_directions" in plan


async def test_repository_create_and_get_plan(env) -> None:
    _factory, repository, _service, _router = env
    plan = await repository.create_plan(
        request_text="需求文本",
        intent="short_video",
        plan_json={"intent": "short_video", "summary": "需求"},
    )
    fetched = await repository.get_plan(plan.id)
    assert fetched is not None and fetched.intent == "short_video"
    assert fetched.status == "draft"


async def test_repository_update_plan(env) -> None:
    _factory, repository, _service, _router = env
    plan = await repository.create_plan(request_text="需求", intent="animation", plan_json={})
    updated = await repository.update_plan(plan.id, status="awaiting_approval", cost_estimate=0.5)
    assert updated.status == "awaiting_approval"
    assert updated.cost_estimate == 0.5


async def test_repository_missing_raises(env) -> None:
    _factory, repository, _service, _router = env
    from pixelle_video.orchestration.repository import ContentPlanNotFoundError

    with pytest.raises(ContentPlanNotFoundError):
        await repository.update_plan("missing", status="approved")


async def test_service_create_plan(env) -> None:
    _factory, repository, service, _router = env
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(
        PlanCreateRequest(request_text="手工皮具钱包Etsy主图", project_id="project-x")
    )
    assert plan.intent == "product_ad"
    assert plan.project_id == "project-x"


@pytest.fixture
async def api_client(env):
    _factory, _repository, service, _router = env
    app = FastAPI()
    app.include_router(orchestration_router, prefix="/api")
    app.dependency_overrides[get_orchestration_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_create_and_get_plan(api_client) -> None:
    client, _factory, _repository, _service = api_client
    created = await client.post("/api/orchestration/plans", json={"request_text": "做一条短视频"})
    assert created.status_code == 201
    body = created.json()
    assert body["intent"] == "short_video"
    assert body["status"] == "draft"

    detail = await client.get(f"/api/orchestration/plans/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["request_text"] == "做一条短视频"


async def test_api_plan_not_found(api_client) -> None:
    client, _factory, _repository, _service = api_client
    response = await client.get("/api/orchestration/plans/missing")
    assert response.status_code == 404


async def test_production_di_generates_to_awaiting_approval(tmp_path, monkeypatch) -> None:
    """D1 修复断言（真实生产 DI，不手工构造）：
    get_orchestration_service() 工厂产物（含 04-A compile 注入）端到端生成，
    状态为 awaiting_approval（非 stage_failed），approval_summary 非空。"""
    import api.dependencies as deps
    from pixelle_video.config.schema import MediaJobsConfig

    url = f"sqlite+aiosqlite:///{(tmp_path / 'di-04e.db').as_posix()}"
    fake_manager = SimpleNamespace(
        config=SimpleNamespace(
            media_jobs=MediaJobsConfig(enabled=True, database_url=url),
            to_dict=lambda: {"comfyui": {}},
        )
    )
    monkeypatch.setattr("api.dependencies.ConfigManager", lambda: fake_manager)
    monkeypatch.setattr(deps, "_orchestration_service", None)
    monkeypatch.setattr(deps, "_media_jobs_database", None)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    service = await get_orchestration_service()
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="手工皮具钱包，Etsy主图"))
    assert plan.intent == "product_ad"
    result = await service.generate(plan.id)
    assert result["status"] == "awaiting_approval"
    assert result["status"] != "stage_failed"
    summary = await service.approval_summary(plan.id)
    assert summary is not None
    assert summary["summary"]
    assert summary["grade"] is not None
