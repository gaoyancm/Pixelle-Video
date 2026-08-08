"""Phase 04-A P1 prompt template storage tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_prompt_service
from api.routers.prompts import router as prompts_router
from api.schemas.prompts import PromptCreate
from api.services.prompts import PromptApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.prompts.repository import (
    PromptNameConflictError,
    PromptNotFoundError,
    PromptRepository,
)


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'prompts.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PromptRepository(factory)
    service = PromptApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


def _create_body(name: str = "商品广告标题", category: str = "广告") -> dict:
    return {
        "name": name,
        "category": category,
        "template_text": "为 {{product_name}} 生成一条吸引人的广告标题",
        "description": "测试模板",
        "variables": [
            {
                "name": "product_name",
                "type": "string",
                "required": True,
                "default": None,
                "description": "商品名称",
            }
        ],
        "provider": "default",
        "change_note": "初始创建",
    }


async def _repo_create(repository: PromptRepository, body: dict):
    variables = body.pop("variables")
    return await repository.create_template(**body, variables_json=variables)


async def test_repository_create_adds_initial_version(env) -> None:
    _factory, repository, _service = env
    template = await _repo_create(repository, _create_body())
    assert template.name == "商品广告标题"
    versions = await repository.list_versions(template.id)
    assert len(versions) == 1
    assert versions[0].version_no == 1
    assert versions[0].template_text == template.template_text


async def test_repository_name_conflict_raises(env) -> None:
    _factory, repository, _service = env
    await _repo_create(repository, _create_body())
    with pytest.raises(PromptNameConflictError):
        await _repo_create(repository, _create_body())


async def test_repository_get_missing_returns_none(env) -> None:
    _factory, repository, _service = env
    assert await repository.get_template("missing") is None


async def test_repository_list_filters_and_pagination(env) -> None:
    _factory, repository, _service = env
    for index in range(5):
        body = _create_body(name=f"模板{index}", category="广告" if index % 2 == 0 else "短视频")
        await _repo_create(repository, body)
    rows, has_more = await repository.list_templates(
        category="广告", is_active=True, limit=2, offset=0
    )
    assert len(rows) == 2 and has_more is True
    all_rows, _ = await repository.list_templates(category=None, is_active=None, limit=10, offset=0)
    assert len(all_rows) == 5


async def test_repository_update_changes_fields_and_versions(env) -> None:
    _factory, repository, _service = env
    template = await _repo_create(repository, _create_body())
    updated = await repository.update_template(
        template.id, name="改名模板", category="短视频", change_note="改名"
    )
    assert updated.name == "改名模板"
    assert updated.category == "短视频"
    versions = await repository.list_versions(template.id)
    assert len(versions) == 2
    assert versions[1].version_no == 2
    assert versions[1].change_note == "改名"


async def test_repository_archive_excludes_from_list(env) -> None:
    _factory, repository, _service = env
    template = await _repo_create(repository, _create_body())
    archived = await repository.archive_template(template.id)
    assert archived.archived_at is not None
    rows, _ = await repository.list_templates(category=None, is_active=None, limit=10, offset=0)
    assert all(row.id != template.id for row in rows)
    with pytest.raises(PromptNotFoundError):
        await repository.update_template(template.id, name="x")


async def test_repository_categories(env) -> None:
    _factory, repository, _service = env
    await _repo_create(repository, _create_body(category="广告"))
    await _repo_create(repository, _create_body(name="旁白模板", category="旁白"))
    categories = await repository.list_categories()
    assert {"广告", "旁白"} == set(categories)


async def test_service_create_and_get(env) -> None:
    _factory, _repository, service = env
    body = PromptCreate.model_validate(_create_body())
    template = await service.create(body)
    fetched = await service.get(template.id)
    assert fetched.id == template.id


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_prompt_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory


async def test_api_create_list_get(api_client) -> None:
    client, factory = api_client
    created = await client.post("/api/admin/prompts", json=_create_body())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "商品广告标题"
    assert body["variables"][0]["name"] == "product_name"

    listing = await client.get("/api/admin/prompts")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == body["id"]

    detail = await client.get(f"/api/admin/prompts/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["category"] == "广告"


async def test_api_patch_and_archive(api_client) -> None:
    client, factory = api_client
    created = (await client.post("/api/admin/prompts", json=_create_body())).json()
    patched = await client.patch(
        f"/api/admin/prompts/{created['id']}",
        json={"name": "改名", "change_note": "改名"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "改名"

    archived = await client.post(f"/api/admin/prompts/{created['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    listing = await client.get("/api/admin/prompts")
    assert all(item["id"] != created["id"] for item in listing.json()["items"])


async def test_api_categories_and_conflict(api_client) -> None:
    client, factory = api_client
    await client.post("/api/admin/prompts", json=_create_body())
    conflict = await client.post("/api/admin/prompts", json=_create_body())
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "name_conflict"
    categories = await client.get("/api/admin/prompts/categories")
    assert categories.status_code == 200
    assert "广告" in categories.json()
