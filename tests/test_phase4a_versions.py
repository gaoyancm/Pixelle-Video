"""Phase 04-A P3 version management and effect tracking tests."""

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
from pixelle_video.prompts.models import PromptTemplate
from pixelle_video.prompts.repository import (
    PromptRepository,
    PromptVersionNotFoundError,
)


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'versions.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PromptRepository(factory)
    service = PromptApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _seed(repository: PromptRepository):
    return await repository.create_template(
        name="版本模板",
        category="广告",
        template_text="旧版本模板文本 {{var1}}",
        variables_json=[
            {"name": "var1", "type": "string", "required": True, "default": None, "description": ""}
        ],
        description="版本测试",
    )


async def test_update_creates_new_version_snapshot(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository)
    updated = await repository.update_template(
        template.id, template_text="新版本模板文本 {{var1}}", change_note="内容更新"
    )
    assert updated.template_text == "新版本模板文本 {{var1}}"
    versions = await repository.list_versions(template.id)
    assert [v.version_no for v in versions] == [1, 2]
    assert versions[0].template_text == "旧版本模板文本 {{var1}}"
    assert versions[1].template_text == "新版本模板文本 {{var1}}"


async def test_rollback_restores_previous_version(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository)
    await repository.update_template(template.id, template_text="第二版 {{var1}}")
    rolled_back = await repository.rollback(template.id, 1, change_note="回到 v1")
    assert rolled_back.template_text == "旧版本模板文本 {{var1}}"
    versions = await repository.list_versions(template.id)
    assert [v.version_no for v in versions] == [1, 2, 3]
    assert versions[2].template_text == "旧版本模板文本 {{var1}}"


async def test_rollback_missing_version_raises(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository)
    with pytest.raises(PromptVersionNotFoundError):
        await repository.rollback(template.id, 99)


async def test_rate_updates_current_score(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository)
    rated = await repository.rate_template(template.id, score=5)
    assert rated.current_score == 5


async def test_rate_with_comment_appends_version(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository)
    await repository.rate_template(template.id, score=4, comment="效果好")
    versions = await repository.list_versions(template.id)
    assert len(versions) == 2
    assert "效果好" in (versions[1].change_note or "")


async def test_rate_invalid_score_raises(env) -> None:
    _factory, repository, _service = env
    template = await _seed(repository)
    with pytest.raises(ValueError):
        await repository.rate_template(template.id, score=6)


async def test_usage_count_increments_on_compile(env) -> None:
    factory, repository, service = env
    template = await _seed(repository)
    await service.compile_prompt(template.id, {"var1": "x"})
    await service.compile_prompt(template.id, {"var1": "y"})
    async with factory() as session:
        stored = await session.get(PromptTemplate, template.id)
    assert stored is not None
    assert stored.usage_count == 2
    assert stored.last_used_at is not None


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


async def test_api_versions_list(api_client) -> None:
    client, _factory, repository = api_client
    template = await _seed(repository)
    await repository.update_template(template.id, template_text="第二版")
    response = await client.get(f"/api/admin/prompts/{template.id}/versions")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["version_no"] for item in items] == [1, 2]


async def test_api_rollback(api_client) -> None:
    client, _factory, repository = api_client
    template = await _seed(repository)
    await repository.update_template(template.id, template_text="第二版")
    response = await client.post(
        f"/api/admin/prompts/{template.id}/rollback", params={"version": 1}
    )
    assert response.status_code == 200
    assert response.json()["template_text"] == "旧版本模板文本 {{var1}}"


async def test_api_rate(api_client) -> None:
    client, _factory, repository = api_client
    template = await _seed(repository)
    response = await client.post(
        f"/api/admin/prompts/{template.id}/rate", json={"score": 4, "comment": "不错"}
    )
    assert response.status_code == 200
    assert response.json()["current_score"] == 4
