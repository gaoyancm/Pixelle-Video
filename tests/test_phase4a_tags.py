"""Phase 04-A P4 tag and search tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_prompt_service
from api.routers.prompts import router as prompts_router
from api.services.prompts import PromptApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.prompts.models import PromptTag
from pixelle_video.prompts.repository import (
    PromptRepository,
    PromptTagNotFoundError,
)


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'tags.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PromptRepository(factory)
    service = PromptApplicationService(repository)
    try:
        async with factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        PromptTag(id=f"tag-{name}", name=name)
                        for name in ("特写", "产品展示", "中式", "TikTok")
                    ]
                )
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _seed(repository: PromptRepository, *, name: str, category: str, text: str):
    return await repository.create_template(
        name=name,
        category=category,
        template_text=text,
        variables_json=[],
        description=f"{name} 描述",
    )


async def test_list_tags_returns_all(env) -> None:
    _factory, repository, _service = env
    tags = await repository.list_tags()
    assert {tag.name for tag in tags} == {"特写", "产品展示", "中式", "TikTok"}


async def test_bind_tags_links_template(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository, name="商品模板", category="广告", text="商品内容")
    bound = await repository.bind_tags(template.id, ["tag-特写", "tag-产品展示"])
    assert {tag.name for tag in bound} == {"特写", "产品展示"}
    linked = await repository.template_tags(template.id)
    assert {tag.name for tag in linked} == {"特写", "产品展示"}


async def test_bind_missing_tag_raises(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository, name="商品模板2", category="广告", text="内容")
    with pytest.raises(PromptTagNotFoundError):
        await repository.bind_tags(template.id, ["tag-不存在"])


async def test_search_by_tag(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository, name="特写商品", category="广告", text="特写展示")
    await repository.bind_tags(template.id, ["tag-特写"])
    rows, _ = await repository.search(tag="特写", category=None, q=None, limit=10, offset=0)
    assert any(row.id == template.id for row in rows)


async def test_search_by_category_and_keyword(env) -> None:
    _factory, repository, _service = env
    await _seed(repository, name="广告标题", category="广告", text="吸引人的标题")
    await _seed(repository, name="动画脚本", category="动画", text="分镜脚本")
    rows, _ = await repository.search(tag=None, category="广告", q="标题", limit=10, offset=0)
    assert len(rows) == 1
    assert rows[0].name == "广告标题"


async def test_search_pagination(env) -> None:
    _factory, repository, _service = env
    for index in range(5):
        await _seed(repository, name=f"模板{index}", category="广告", text=f"内容{index}")
    rows, has_more = await repository.search(tag=None, category="广告", q=None, limit=2, offset=0)
    assert len(rows) == 2 and has_more is True


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_prompt_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository


async def test_api_tags_list_and_bind(api_client) -> None:
    client, _factory, repository = api_client
    template = await _seed(repository, name="绑定模板", category="广告", text="内容")
    tags_response = await client.get("/api/admin/prompts/tags")
    assert tags_response.status_code == 200
    assert {item["name"] for item in tags_response.json()["items"]} == {
        "特写",
        "产品展示",
        "中式",
        "TikTok",
    }
    bind = await client.post(
        f"/api/admin/prompts/{template.id}/tags", json={"tag_ids": ["tag-中式"]}
    )
    assert bind.status_code == 200
    assert bind.json()["items"][0]["name"] == "中式"


async def test_api_search_combines_tag_category_and_q(api_client) -> None:
    client, _factory, repository = api_client
    template = await _seed(repository, name="竖屏特写广告", category="广告", text="竖屏特写产品")
    await repository.bind_tags(template.id, ["tag-特写"])
    response = await client.get(
        "/api/admin/prompts/search",
        params={"tag": "特写", "category": "广告", "q": "竖屏"},
    )
    assert response.status_code == 200
    assert any(item["id"] == template.id for item in response.json()["items"])
