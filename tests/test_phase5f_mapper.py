"""Phase 05-F A1 content-plan-to-brief mapping tests."""

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
from pixelle_video.orchestration.models import ContentPlan
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.products.brief_mapper import BriefMapper
from pixelle_video.products.repository import ProductBriefRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    plan_repository = ContentPlanRepository(factory)
    service = ProductApplicationService(
        brief_repository,
        plan_repository=plan_repository,
        brief_mapper=BriefMapper(),
    )
    try:
        yield factory, brief_repository, plan_repository, service
    finally:
        await engine.dispose()


async def _make_plan(plan_repository: ContentPlanRepository, **extra) -> ContentPlan:
    return await plan_repository.create_plan(
        request_text="手工皮具钱包，Etsy主图+TikTok广告",
        intent="product_ad",
        plan_json={
            "intent": "product_ad",
            "summary": "为手工皮具钱包生成Etsy主图和TikTok广告",
            "target_audience": "25-45岁追求品质的女性",
            "platforms": ["etsy", "tiktok"],
            "creative_directions": [{"hook": "意大利头层牛皮", "angle": "品质感奢华风"}],
            "visual_style": {"palette": "温暖皮革色系", "mood": "精致"},
            "tasks": [{"type": "image", "template": "product_main", "count": 1}],
        },
        **extra,
    )


async def test_mapper_maps_core_fields(env) -> None:
    _factory, _briefs, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = BriefMapper().map(plan)
    assert mapped["description"].startswith("为手工皮具钱包生成Etsy主图和TikTok广告")
    assert mapped["target_audience"] == "25-45岁追求品质的女性"
    assert mapped["platforms_json"] == ["etsy", "tiktok"]
    assert "温暖皮革色系" in mapped["selling_points_json"]


async def test_mapper_establishes_plan_trace(env) -> None:
    _factory, _briefs, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = BriefMapper().map(plan)
    assert mapped["plan_id"] == plan.id
    assert f"[source_plan: {plan.id}]" in mapped["description"]


async def test_mapper_carries_creative_directions(env) -> None:
    _factory, _briefs, plan_repository, _service = env
    plan = await _make_plan(plan_repository)
    mapped = BriefMapper().map(plan)
    meta = mapped["reference_images_json"][0]
    assert meta["plan_id"] == plan.id
    assert meta["creative_directions"][0]["angle"] == "品质感奢华风"


async def test_service_create_brief_from_plan(env) -> None:
    _factory, brief_repository, plan_repository, service = env
    plan = await _make_plan(plan_repository)
    payload = await service.create_brief_from_plan(plan.id)
    assert payload["plan_id"] == plan.id
    assert payload["target_audience"] == "25-45岁追求品质的女性"
    brief = await brief_repository.get_brief(payload["brief_id"])
    assert brief is not None and brief.product_name == "手工皮具钱包"


async def test_service_plan_trace_back(env) -> None:
    _factory, _briefs, plan_repository, service = env
    plan = await _make_plan(plan_repository)
    brief_payload = await service.create_brief_from_plan(plan.id)
    traced = await service.get_plan_for_brief(brief_payload["brief_id"])
    assert traced is not None
    assert traced["plan_id"] == plan.id
    assert traced["intent"] == "product_ad"


async def test_service_trace_missing_plan_raises(env) -> None:
    _factory, _briefs, plan_repository, service = env
    from pixelle_video.orchestration.repository import ContentPlanNotFoundError

    with pytest.raises(ContentPlanNotFoundError):
        await service.create_brief_from_plan("missing-plan")


async def test_api_from_plan_and_trace(api_client) -> None:
    client, _factory, _briefs, plan_repository, _service = api_client
    plan = await _make_plan(plan_repository)
    created = await client.post(f"/api/products/briefs/from-plan/{plan.id}")
    assert created.status_code == 201
    body = created.json()
    assert body["plan_id"] == plan.id
    assert body["product_name"] == "手工皮具钱包"

    traced = await client.get(f"/api/products/briefs/{body['brief_id']}/plan")
    assert traced.status_code == 200
    assert traced.json()["intent"] == "product_ad"


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
