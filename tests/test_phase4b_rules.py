"""Phase 04-B Q1 QC rule storage tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_qc_service
from api.routers.qc import router as qc_router
from api.services.qc import QCApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.qc.executor import QCExecutor
from pixelle_video.qc.repository import (
    QCProfileNameConflictError,
    QCRepository,
    QCRuleNameConflictError,
    QCRuleNotFoundError,
)


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'qc.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = QCRepository(factory)
    service = QCApplicationService(repository, QCExecutor(repository))
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


def _rule_body(name: str = "分辨率检查", category: str = "technical", priority: int = 1) -> dict:
    return {
        "name": name,
        "category": category,
        "rule_type": "schema_validation",
        "rule_config": {
            "field": "resolution",
            "operator": "min",
            "expected": "1280x720",
            "severity": "major",
        },
        "provider": "ffprobe",
        "priority": priority,
    }


async def _repo_create(repository, body):
    config = body.pop("rule_config")
    return await repository.create_rule(**body, rule_config_json=config)


async def test_repository_create_rule(env) -> None:
    _factory, repository, _service = env
    rule = await _repo_create(repository, _rule_body())
    assert rule.category == "technical"
    assert rule.rule_config_json["field"] == "resolution"


async def test_repository_rule_name_conflict(env) -> None:
    _factory, repository, _service = env
    await _repo_create(repository, _rule_body())
    with pytest.raises(QCRuleNameConflictError):
        await _repo_create(repository, _rule_body())


async def test_repository_list_rules_filters_by_category(env) -> None:
    _factory, repository, _service = env
    await _repo_create(repository, _rule_body())
    await _repo_create(repository, _rule_body(name="画面稳定性", category="visual", priority=5))
    technical = await repository.list_rules(category="technical")
    visual = await repository.list_rules(category="visual")
    assert len(technical) == 1 and len(visual) == 1
    all_rules = await repository.list_rules()
    assert len(all_rules) == 2


async def test_repository_update_rule(env) -> None:
    _factory, repository, _service = env
    rule = await _repo_create(repository, _rule_body())
    updated = await repository.update_rule(rule.id, is_active=0, priority=9)
    assert updated.is_active == 0 and updated.priority == 9


async def test_repository_update_missing_rule_raises(env) -> None:
    _factory, repository, _service = env
    with pytest.raises(QCRuleNotFoundError):
        await repository.update_rule("missing", name="x")


async def test_repository_create_and_list_profiles(env) -> None:
    _factory, repository, _service = env
    profile = await repository.create_profile(
        name="测试方案",
        rules_json=[{"rule_id": "r1", "severity_override": None}],
        is_default=1,
    )
    assert profile.is_default == 1
    profiles = await repository.list_profiles()
    assert [p.name for p in profiles] == ["测试方案"]


async def test_repository_profile_name_conflict(env) -> None:
    _factory, repository, _service = env
    await repository.create_profile(name="方案A", rules_json=[])
    with pytest.raises(QCProfileNameConflictError):
        await repository.create_profile(name="方案A", rules_json=[])


async def test_repository_default_profile(env) -> None:
    _factory, repository, _service = env
    await repository.create_profile(name="方案A", rules_json=[], is_default=1)
    default = await repository.get_default_profile()
    assert default is not None and default.name == "方案A"


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(qc_router, prefix="/api")
    app.dependency_overrides[get_qc_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory


async def test_api_rule_crud(api_client) -> None:
    client, _factory = api_client
    created = await client.post("/api/admin/qc/rules", json=_rule_body())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "分辨率检查"

    listing = await client.get("/api/admin/qc/rules", params={"category": "technical"})
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1

    patched = await client.patch(f"/api/admin/qc/rules/{body['id']}", json={"is_active": False})
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False


async def test_api_profile_create_list(api_client) -> None:
    client, _factory = api_client
    created = await client.post(
        "/api/admin/qc/profiles",
        json={"name": "广告QC", "rules": [{"rule_id": "r1"}], "is_default": True},
    )
    assert created.status_code == 201
    assert created.json()["is_default"] is True
    listing = await client.get("/api/admin/qc/profiles")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["name"] == "广告QC"


async def test_api_rule_conflict(api_client) -> None:
    client, _factory = api_client
    await client.post("/api/admin/qc/rules", json=_rule_body())
    conflict = await client.post("/api/admin/qc/rules", json=_rule_body())
    assert conflict.status_code == 409
