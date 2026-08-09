"""Phase 04-D K2 source tracing and K3 link/search tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_knowledge_service
from api.routers.knowledge import router as knowledge_router
from api.schemas.knowledge import KnowledgeEntryCreate, KnowledgeEntryUpdate
from api.services.knowledge import KnowledgeApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.knowledge.repository import KnowledgeRepository
from pixelle_video.media_jobs.models import Base


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'k23.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = KnowledgeRepository(factory)
    audit = AuditRepository(factory)
    service = KnowledgeApplicationService(repository, audit=audit)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


def _body(**extra) -> dict:
    body = {
        "title": "知识条目",
        "content": "正文内容",
        "category": "策划",
        "evidence_class": "production_heuristic",
        "status": "published",
    }
    body.update(extra)
    return body


# --- K2: source tracing ---------------------------------------------------------


async def test_k2_stale_detection_older_than_six_months(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_body())
    old = datetime.now(timezone.utc) - timedelta(days=200)
    await repository.mark_verified(entry.id, verified_at=old)
    assert repository.is_stale(entry) is True


async def test_k2_fresh_verified_entry_not_stale(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_body())
    await repository.mark_verified(entry.id)
    fresh = await repository.get_entry(entry.id)
    assert repository.is_stale(fresh) is False


async def test_k2_verify_updates_verified_at(env) -> None:
    _factory, repository, service = env
    entry = await repository.create_entry(**_body())
    verified = await service.verify(entry.id)
    assert verified.verified_at is not None


async def test_k2_stale_list_returns_unverified_and_old(env) -> None:
    _factory, repository, _service = env
    await repository.create_entry(**_body(title="未验证"))
    old_entry = await repository.create_entry(**_body(title="过期"))
    old = datetime.now(timezone.utc) - timedelta(days=300)
    await repository.mark_verified(old_entry.id, verified_at=old)
    stale = await repository.list_stale()
    titles = {entry.title for entry in stale}
    assert "未验证" in titles
    assert "过期" in titles


async def test_k2_changes_written_to_audit(env) -> None:
    _factory, repository, service = env
    entry = await service.create(KnowledgeEntryCreate(**_body(title="审计条目")))
    await service.update(
        entry.id,
        KnowledgeEntryUpdate(title="审计条目V2", evidence_class="production_heuristic"),
    )
    events, _ = await service.audit.list(scope_type="knowledge_entry", scope_id=entry.id)
    event_types = {event.event_type for event in events}
    assert "knowledge_created" in event_types
    assert "knowledge_updated" in event_types


async def test_k2_stale_api(api_env) -> None:
    client, repository, _service = api_env
    await repository.create_entry(**_body(title="待验证条目"))
    response = await client.get("/api/admin/knowledge/stale")
    assert response.status_code == 200
    assert any(item["title"] == "待验证条目" for item in response.json()["items"])


@pytest.fixture
async def api_env(env):
    _factory, repository, service = env
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api")
    app.dependency_overrides[get_knowledge_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, repository, service

