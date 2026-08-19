"""Phase 05 A1 product brief model & API tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_product_service
from api.routers.products import router as products_router
from api.services.products import ProductApplicationService
from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.products.ad_engine import AdProductionEngine
from pixelle_video.products.repository import ProductBriefRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    job_repository = MediaJobRepository(factory)
    management_repository = ManagementRepository(factory)
    await management_repository.create_project(name="Test project", project_id="project-x")
    asset_repository = AssetRepository(factory)
    ad_engine = AdProductionEngine(
        brief_repository, management_repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    service = ProductApplicationService(
        brief_repository,
        ad_engine=ad_engine,
        asset_repository=asset_repository,
        job_repository=job_repository,
    )
    try:
        yield factory, brief_repository, job_repository, management_repository, service
    finally:
        await engine.dispose()


def _brief_body(**extra) -> dict:
    body = {
        "product_name": "手工皮具钱包",
        "description": "意大利头层牛皮手工缝制，简约耐用",
        "category": "时尚配件",
        "selling_points": ["意大利头层牛皮", "手工缝制", "多卡位设计"],
        "target_audience": "25-45岁追求品质的女性",
        "platforms": ["etsy", "tiktok", "instagram"],
        "project_id": "project-x",
    }
    body.update(extra)
    return body


async def test_repository_create_brief(env) -> None:
    _factory, repository, _jobs, _management, _service = env
    brief = await repository.create_brief(**_brief_body())
    assert brief.product_name == "手工皮具钱包"
    assert brief.selling_points_json == ["意大利头层牛皮", "手工缝制", "多卡位设计"]
    assert brief.platforms_json == ["etsy", "tiktok", "instagram"]
    assert brief.status == "draft"


async def test_repository_get_and_list(env) -> None:
    _factory, repository, _jobs, _management, _service = env
    brief = await repository.create_brief(**_brief_body())
    fetched = await repository.get_brief(brief.id)
    assert fetched is not None and fetched.category == "时尚配件"
    rows, _ = await repository.list_briefs(limit=10, offset=0)
    assert len(rows) == 1
    rows, _ = await repository.list_briefs(project_id=brief.project_id, limit=10, offset=0)
    assert len(rows) == 1


async def test_repository_update_status(env) -> None:
    _factory, repository, _jobs, _management, _service = env
    brief = await repository.create_brief(**_brief_body())
    updated = await repository.update_status(brief.id, "processing")
    assert updated.status == "processing"


async def test_repository_update_fields(env) -> None:
    _factory, repository, _jobs, _management, _service = env
    brief = await repository.create_brief(**_brief_body())
    updated = await repository.update_brief(
        brief.id, product_name="新名称", selling_points=["新卖点"]
    )
    assert updated.product_name == "新名称"
    assert updated.selling_points_json == ["新卖点"]


async def test_service_generate_ideas(env) -> None:
    _factory, repository, _jobs, _management, service = env
    brief = await repository.create_brief(**_brief_body())
    payload = await service.generate_ideas(brief.id)
    assert payload["brief_id"] == brief.id
    assert len(payload["ideas"]) == 3
    for idea in payload["ideas"]:
        assert idea["hook"] and idea["headline"] and idea["cta"] and idea["style"]


@pytest.fixture
async def api_client(env):
    _factory, _repository, _jobs, _management, service = env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, _jobs, _management, service


async def test_api_create_and_list(api_client) -> None:
    client, _factory, _repository, _jobs, _management, _service = api_client
    created = await client.post("/api/products/briefs", json=_brief_body())
    assert created.status_code == 201
    body = created.json()
    assert body["product_name"] == "手工皮具钱包"
    assert body["status"] == "draft"
    assert body["selling_points"] == ["意大利头层牛皮", "手工缝制", "多卡位设计"]

    listing = await client.get("/api/products/briefs")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == body["id"]


async def test_api_create_requires_project_id(api_client) -> None:
    client, _factory, _repository, _jobs, _management, _service = api_client
    payload = _brief_body()
    payload.pop("project_id")
    response = await client.post("/api/products/briefs", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_api_get_detail(api_client) -> None:
    client, _factory, repository, _jobs, _management, _service = api_client
    brief = await repository.create_brief(**_brief_body())
    detail = await client.get(f"/api/products/briefs/{brief.id}")
    assert detail.status_code == 200
    assert detail.json()["category"] == "时尚配件"


async def test_api_update(api_client) -> None:
    client, _factory, repository, _jobs, _management, _service = api_client
    brief = await repository.create_brief(**_brief_body())
    updated = await client.patch(
        f"/api/products/briefs/{brief.id}",
        json={"product_name": "升级版钱包", "status": "submitted"},
    )
    assert updated.status_code == 200
    assert updated.json()["product_name"] == "升级版钱包"
    assert updated.json()["status"] == "submitted"


async def test_api_generate_ideas(api_client) -> None:
    client, _factory, repository, _jobs, _management, _service = api_client
    brief = await repository.create_brief(**_brief_body())
    response = await client.post(f"/api/products/briefs/{brief.id}/generate-ideas")
    assert response.status_code == 200
    assert len(response.json()["ideas"]) == 3


async def test_api_not_found(api_client) -> None:
    client, _factory, _repository, _jobs, _management, _service = api_client
    response = await client.get("/api/products/briefs/missing")
    assert response.status_code == 404


async def test_generate_ideas_uses_injected_04a_compiler(env) -> None:
    """D1: generate-ideas must compile through the injected 04-A compiler."""
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, _jobs, _management, _service = env
    _service.prompt_compiler = prompt_compile
    brief = await repository.create_brief(**_brief_body())
    payload = await _service.generate_ideas(brief.id)
    for idea in payload["ideas"]:
        # The double-brace placeholders must have been replaced by compile().
        assert "{{product}}" not in idea["hook"]
        assert "{{points}}" not in idea["hook"]
        assert "手工皮具钱包" in idea["hook"]


async def test_api_generate_ideas_with_compiler_injection(api_client) -> None:
    """D1: API path honours the injected 04-A compiler (no braces leak)."""
    from pixelle_video.prompts.compiler import compile as prompt_compile

    client, _factory, repository, _jobs, _management, service = api_client
    service.prompt_compiler = prompt_compile
    brief = await repository.create_brief(**_brief_body())
    response = await client.post(f"/api/products/briefs/{brief.id}/generate-ideas")
    assert response.status_code == 200
    for idea in response.json()["ideas"]:
        assert "{{" not in idea["hook"]
