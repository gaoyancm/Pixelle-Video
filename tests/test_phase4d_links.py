"""Phase 04-D K3 knowledge link and search tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_knowledge_service
from api.routers.knowledge import router as knowledge_router
from api.services.knowledge import KnowledgeApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.knowledge.repository import KnowledgeRepository
from pixelle_video.media_jobs.models import Base


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'k3.db').as_posix()}")
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


# --- K3: links & search -----------------------------------------------------------


async def test_k3_repository_add_and_list_links(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_body())
    link = await repository.add_link(
        entry.id, target_type="prompt_template", target_id="pt-1", link_note="借鉴"
    )
    assert link.target_type == "prompt_template"
    links = await repository.list_links(entry.id)
    assert len(links) == 1
    assert links[0].target_id == "pt-1"


async def test_k3_link_missing_entry_raises(env) -> None:
    _factory, repository, _service = env
    from pixelle_video.knowledge.repository import KnowledgeEntryNotFoundError

    with pytest.raises(KnowledgeEntryNotFoundError):
        await repository.add_link("missing", target_type="qc_rule", target_id="qc-1")


async def test_k3_search_by_keyword(env) -> None:
    _factory, repository, _service = env
    await repository.create_entry(**_body(title="TikTok开头Hook技巧", content="前 3 秒抓注意力"))
    await repository.create_entry(
        **_body(title="商品展示", category="品牌产品", content="特写镜头")
    )
    rows, _ = await repository.search(query="Hook", limit=10, offset=0)
    assert len(rows) == 1
    assert rows[0].title == "TikTok开头Hook技巧"


async def test_k3_search_by_tag_and_category(env) -> None:
    _factory, repository, _service = env
    await repository.create_entry(**_body(title="带标签条目", tags=["竖屏"]))
    await repository.create_entry(**_body(title="其他条目", tags=["镜头"]))
    rows, _ = await repository.search(tag="竖屏", limit=10, offset=0)
    assert [row.title for row in rows] == ["带标签条目"]
    rows, _ = await repository.search(category="策划", limit=10, offset=0)
    assert len(rows) == 2


async def test_k3_search_excludes_archived(env) -> None:
    _factory, repository, _service = env
    entry = await repository.create_entry(**_body(title="已归档条目"))
    await repository.archive_entry(entry.id)
    rows, _ = await repository.search(query="已归档", limit=10, offset=0)
    assert rows == []


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


async def test_k3_link_api(api_env) -> None:
    client, repository, _service = api_env
    entry = await repository.create_entry(**_body())
    linked = await client.post(
        f"/api/admin/knowledge/{entry.id}/link",
        json={"target_type": "qc_rule", "target_id": "qc-resolution", "link_note": "关联检查"},
    )
    assert linked.status_code == 201
    assert linked.json()["target_type"] == "qc_rule"
    links = await client.get(f"/api/admin/knowledge/{entry.id}/links")
    assert links.status_code == 200
    assert len(links.json()["items"]) == 1


async def test_k3_search_api(api_env) -> None:
    client, repository, _service = api_env
    await repository.create_entry(**_body(title="TikTok开头Hook技巧", tags=["TikTok"]))
    response = await client.get(
        "/api/admin/knowledge/search", params={"q": "Hook", "tag": "TikTok"}
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
