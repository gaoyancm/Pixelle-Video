"""Phase 04-D K1 knowledge entry CRUD tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_knowledge_service
from api.routers.knowledge import router as knowledge_router
from api.services.knowledge import KnowledgeApplicationService
from pixelle_video.knowledge.repository import (
    KnowledgeEntryNotFoundError,
    KnowledgeRepository,
    KnowledgeValidationError,
)
from pixelle_video.media_jobs.models import Base


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'kb.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = KnowledgeRepository(factory)
    service = KnowledgeApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


def _entry_body(
    title: str = "TikTok开头Hook技巧",
    category: str = "策划",
    evidence_class: str = "production_heuristic",
    **extra,
) -> dict:
    body = {
        "title": title,
        "content": "前 1-2 秒抓住注意力。",
        "category": category,
        "evidence_class": evidence_class,
        "status": "published",
        "tags": ["TikTok", "Hook"],
    }
    body.update(extra)
    return body


async def test_repository_create_entry(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_entry_body())
    assert entry.category == "策划"
    assert entry.evidence_class == "production_heuristic"
    assert entry.status == "published"


async def test_repository_create_entry_with_tags(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_entry_body(tags=["TikTok", "竖屏", "视频"]))
    tags = await repository.tags_for_entry(entry.id)
    assert {tag.name for tag in tags} == {"TikTok", "竖屏", "视频"}


async def test_repository_get_and_list(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_entry_body())
    fetched = await repository.get_entry(entry.id)
    assert fetched is not None and fetched.title == "TikTok开头Hook技巧"
    rows, _ = await repository.list_entries(limit=10, offset=0)
    assert len(rows) == 1


async def test_repository_list_filters_by_category_and_status(env) -> None:
    _factory, repository, _service = env
    await repository.create_entry(**_entry_body())
    await repository.create_entry(
        **_entry_body(title="平台规格", category="平台规则", tags=["视频"])
    )
    rows, _ = await repository.list_entries(category="策划", limit=10, offset=0)
    assert len(rows) == 1
    archived = await repository.create_entry(**_entry_body(title="草稿条目", status="draft"))
    await repository.archive_entry(archived.id)
    rows, _ = await repository.list_entries(status="archived", limit=10, offset=0)
    assert len(rows) == 1


async def test_repository_update_entry(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_entry_body())
    updated = await repository.update_entry(entry.id, title="新标题", status="draft")
    assert updated.title == "新标题" and updated.status == "draft"


async def test_repository_archive_and_missing(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_entry_body())
    archived = await repository.archive_entry(entry.id)
    assert archived.status == "archived"
    with pytest.raises(KnowledgeEntryNotFoundError):
        await repository.archive_entry("missing")


async def test_repository_evidence_class_validation(env) -> None:
    _factory, repository, _service = env
    # documented_fact without any source must raise.
    with pytest.raises(KnowledgeValidationError):
        await repository.create_entry(
            **_entry_body(evidence_class="documented_fact", source_url=None, source_doc=None)
        )
    # documented_fact with source_doc is fine.
    entry = await repository.create_entry(
        **_entry_body(
            evidence_class="documented_fact",
            source_url=None,
            source_doc="勘测报告",
        )
    )
    assert entry.evidence_class == "documented_fact"


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api")
    app.dependency_overrides[get_knowledge_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_entry_crud(api_client) -> None:
    client, _factory, _repository, _service = api_client
    created = await client.post("/api/admin/knowledge", json=_entry_body())
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "TikTok开头Hook技巧"
    assert body["tags"] == ["TikTok", "Hook"]

    listing = await client.get("/api/admin/knowledge", params={"category": "策划"})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == body["id"]

    detail = await client.get(f"/api/admin/knowledge/{body['id']}")
    assert detail.status_code == 200
    # Unverified entries are flagged stale until verified.
    assert detail.json()["stale"] is True

    updated = await client.patch(f"/api/admin/knowledge/{body['id']}", json={"status": "archived"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "archived"


async def test_api_validation_error_for_documented_fact(api_client) -> None:
    client, _factory, _repository, _service = api_client
    response = await client.post(
        "/api/admin/knowledge",
        json=_entry_body(evidence_class="documented_fact", source_url=None, source_doc=None),
    )
    assert response.status_code == 422
