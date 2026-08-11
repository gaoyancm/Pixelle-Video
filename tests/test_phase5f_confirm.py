"""Phase 05-F A3 confirm-loop tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_product_service
from api.routers.products import router as products_router
from api.services.products import ProductApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.agents.sub_agents import Copywriter
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.products.brief_mapper import BriefMapper
from pixelle_video.products.repository import ProductBriefRepository
from tests.test_phase5_briefs import _brief_body
from tests.test_phase5f_ideas import _mock_copywriter_llm


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    service = ProductApplicationService(
        brief_repository,
        copywriter_agent=Copywriter(_mock_copywriter_llm),
        plan_repository=plan_repository,
        brief_mapper=BriefMapper(),
    )
    try:
        yield factory, brief_repository, plan_repository, service
    finally:
        await engine.dispose()


async def test_confirm_from_plan_without_engine_raises(env) -> None:
    _factory, brief_repository, _plans, service = env
    brief = await brief_repository.create_brief(**_brief_body())
    with pytest.raises(RuntimeError, match="ad engine"):
        await service.confirm_from_plan(brief.id)


async def test_confirm_from_plan_with_mock_engine(env) -> None:
    _factory, brief_repository, _plans, service = env

    class MockEngine:
        async def start_production(self, brief_id: str) -> dict:
            return {"production": "started", "brief_id": brief_id}

    service.ad_engine = MockEngine()
    brief = await brief_repository.create_brief(**_brief_body())
    payload = await service.confirm_from_plan(brief.id)
    assert payload["status"] == "processing"
    assert payload["production"] == "started"
    assert payload["source"] == "04-e-copywriter"
    assert len(payload["ideas"]) == 3
    updated = await brief_repository.get_brief(brief.id)
    assert updated.status == "processing"


async def test_confirm_keeps_plan_link(env) -> None:
    _factory, brief_repository, plan_repository, service = env
    plan = await plan_repository.create_plan(
        request_text="手工皮具钱包",
        intent="product_ad",
        plan_json={
            "summary": "手工皮具钱包主图",
            "target_audience": "女性",
            "platforms": ["etsy"],
            "creative_directions": [{"angle": "奢华风"}],
            "visual_style": {},
        },
    )
    brief_payload = await service.create_brief_from_plan(plan.id)
    assert brief_payload["plan_id"] == plan.id
    traced = await service.get_plan_for_brief(brief_payload["brief_id"])
    assert traced is not None and traced["intent"] == "product_ad"


async def test_confirm_from_plan_idea_uses_plan_meta(env) -> None:
    _factory, brief_repository, plan_repository, service = env
    plan = await plan_repository.create_plan(
        request_text="手工皮具钱包，Etsy",
        intent="product_ad",
        plan_json={
            "summary": "手工皮具钱包",
            "target_audience": "女性",
            "platforms": ["etsy"],
            "creative_directions": [{"angle": "奢华风"}],
            "visual_style": {"palette": "暖色"},
        },
    )
    brief_payload = await service.create_brief_from_plan(plan.id)

    class MockEngine:
        async def start_production(self, brief_id: str) -> dict:
            return {"batch_id": "batch-1", "tasks": 1, "jobs": {"image": ["job-1"]}}

    service.ad_engine = MockEngine()
    payload = await service.confirm_from_plan(brief_payload["brief_id"])
    assert payload["ideas"][0]["hook"] == "AI Hook 1"


@pytest.fixture
async def api_client(env):
    _factory, _briefs, _plans, service = env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _briefs, _plans, service


async def test_api_confirm_from_plan(api_client) -> None:
    client, _factory, brief_repository, _plans, service = api_client

    class MockEngine:
        async def start_production(self, brief_id: str) -> dict:
            return {"batch_id": "batch-1", "tasks": 1, "jobs": {"image": ["job-1"]}}

    service.ad_engine = MockEngine()
    brief = await brief_repository.create_brief(**_brief_body())
    response = await client.post(f"/api/products/briefs/{brief.id}/confirm-from-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["batch_id"] == "batch-1"
    assert len(body["ideas"]) == 3


async def test_api_full_plan_to_production(api_client) -> None:
    """A3 end-to-end: plan -> brief -> confirm-from-plan -> production."""
    client, _factory, _briefs, plan_repository, service = api_client
    plan = await plan_repository.create_plan(
        request_text="手工皮具钱包，Etsy主图",
        intent="product_ad",
        plan_json={
            "summary": "手工皮具钱包主图",
            "target_audience": "25-45岁女性",
            "platforms": ["etsy", "tiktok"],
            "creative_directions": [{"hook": "意大利头层牛皮"}],
            "visual_style": {"palette": "暖色"},
        },
    )
    created = await client.post(f"/api/products/briefs/from-plan/{plan.id}")
    assert created.status_code == 201
    brief_id = created.json()["brief_id"]

    class MockEngine:
        async def start_production(self, brief_id: str) -> dict:
            return {"batch_id": "batch-1", "tasks": 1, "jobs": {"image": ["job-1"]}}

    service.ad_engine = MockEngine()
    confirmed = await client.post(f"/api/products/briefs/{brief_id}/confirm-from-plan")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "processing"
    assert confirmed.json()["source"] == "04-e-copywriter"
