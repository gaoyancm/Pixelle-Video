"""Phase 03-F F4 budget and cost control tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_budget_service, get_media_job_service
from api.routers.audit_budget import router as audit_budget_router
from api.routers.media_jobs import router as media_jobs_router
from api.services.media_jobs import MediaJobApplicationService
from pixelle_video.budget import (
    BudgetBlockedError,
    BudgetConfigurationError,
    BudgetRepository,
    BudgetService,
)
from pixelle_video.config.schema import MediaJobsConfig
from pixelle_video.media_jobs.models import Base, MediaJob
from pixelle_video.media_jobs.repository import MediaJobRepository

FIXED_ESTIMATE = 5.0


def node_id_for_workflow(workflow: str) -> str:
    return "a800"


@pytest.fixture
async def sessions(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'budget.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _configure(
    sessions, mode: str, per_task_limit=None, per_batch_limit=None
) -> BudgetService:
    budget = BudgetService(
        BudgetRepository(sessions),
        sessions,
        estimate=lambda workflow: FIXED_ESTIMATE,
        job_repository=MediaJobRepository(sessions),
    )
    await budget.update_config(
        per_task_limit=per_task_limit,
        per_batch_limit=per_batch_limit,
        mode=mode,
    )
    return budget


# --- Pure service behavior ---------------------------------------------------


async def test_observe_mode_does_not_block_or_warn(sessions) -> None:
    budget = await _configure(sessions, "observe", per_task_limit=1.0)
    decision = await budget.check_job_creation("a800_wan22_t2v_33f")
    assert decision.allowed is True
    assert decision.warning is None
    assert decision.estimated_cost == FIXED_ESTIMATE


async def test_warn_mode_returns_warning_but_allows(sessions) -> None:
    budget = await _configure(sessions, "warn", per_task_limit=1.0)
    decision = await budget.check_job_creation("a800_wan22_t2v_33f")
    assert decision.allowed is True
    assert decision.warning is not None
    assert "exceeds per-task limit" in decision.warning


async def test_cap_mode_blocks_over_limit(sessions) -> None:
    budget = await _configure(sessions, "cap", per_task_limit=1.0)
    with pytest.raises(BudgetBlockedError) as exc:
        await budget.check_job_creation("a800_wan22_t2v_33f")
    assert exc.value.limit == 1.0
    assert exc.value.estimated == FIXED_ESTIMATE


async def test_under_limit_is_allowed_in_all_modes(sessions) -> None:
    for mode in ("observe", "warn", "cap"):
        budget = await _configure(sessions, mode, per_task_limit=10.0)
        decision = await budget.check_job_creation("a800_wan22_t2v_33f")
        assert decision.allowed is True
        assert decision.warning is None


async def test_batch_limit_cap_blocks(sessions) -> None:
    budget = await _configure(sessions, "cap", per_batch_limit=8.0)
    with pytest.raises(BudgetBlockedError):
        await budget.check_batch_submission("a800_wan22_t2v_33f", item_count=2)


async def test_update_config_validates_mode_and_limits(sessions) -> None:
    budget = await _configure(sessions, "observe")
    with pytest.raises(BudgetConfigurationError):
        await budget.update_config(per_task_limit=None, per_batch_limit=None, mode="invalid")
    with pytest.raises(BudgetConfigurationError):
        await budget.update_config(per_task_limit=-1, per_batch_limit=None, mode="observe")


async def test_config_persists_across_reads(sessions) -> None:
    budget = await _configure(sessions, "warn", per_task_limit=3.0)
    config = await budget.get_config()
    assert config.mode == "warn"
    assert config.per_task_limit == 3.0


async def test_usage_aggregates_estimated_costs(sessions) -> None:
    budget = await _configure(sessions, "observe")
    usage = await budget.usage("project-1")
    assert usage["project_id"] == "project-1"
    assert usage["job_count"] == 0
    assert usage["estimated_total"] == 0.0
    assert usage["mode"] == "observe"


# --- API behavior ------------------------------------------------------------


@pytest.fixture
async def job_api_client(sessions):
    repository = MediaJobRepository(sessions)
    budget = BudgetService(
        BudgetRepository(sessions),
        sessions,
        estimate=lambda workflow: FIXED_ESTIMATE,
        job_repository=repository,
    )
    await budget.update_config(per_task_limit=1.0, per_batch_limit=None, mode="cap")
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for_workflow,
        budget=budget,
    )
    app = FastAPI()
    app.include_router(media_jobs_router, prefix="/api")
    app.include_router(audit_budget_router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service
    app.dependency_overrides[get_budget_service] = lambda: budget
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, service, sessions


async def test_cap_mode_create_job_returns_429(job_api_client) -> None:
    client, _, _ = job_api_client
    response = await client.post(
        "/api/media/jobs",
        json={"workflow": "a800_wan22_t2v_33f", "parameters": {"prompt": "safe prompt"}},
        headers={"Idempotency-Key": "cap-block"},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "budget_limit_exceeded"


async def test_cap_mode_allows_under_limit_create(job_api_client) -> None:
    client, _, _ = job_api_client
    low_budget = await client.patch(
        "/api/admin/budget/config", json={"per_task_limit": 10.0, "mode": "cap"}
    )
    assert low_budget.status_code == 200
    response = await client.post(
        "/api/media/jobs",
        json={"workflow": "a800_wan22_t2v_33f", "parameters": {"prompt": "safe prompt"}},
        headers={"Idempotency-Key": "cap-ok"},
    )
    assert response.status_code == 201


async def test_warn_mode_marks_job_warning(job_api_client) -> None:
    client, service, sessions = job_api_client
    await client.patch(
        "/api/admin/budget/config",
        json={"per_task_limit": 1.0, "per_batch_limit": None, "mode": "warn"},
    )
    response = await client.post(
        "/api/media/jobs",
        json={"workflow": "a800_wan22_t2v_33f", "parameters": {"prompt": "safe prompt"}},
        headers={"Idempotency-Key": "warn-key"},
    )
    assert response.status_code == 201
    job_id = response.json()["job_id"]
    async with sessions() as session:
        stored = await session.get(MediaJob, job_id)
    assert stored is not None
    assert stored.estimated_cost == FIXED_ESTIMATE
    assert stored.budget_warning is not None


async def test_budget_config_api_get_patch(sessions) -> None:
    budget = await _configure(sessions, "observe")
    app = FastAPI()
    app.include_router(audit_budget_router, prefix="/api")
    app.dependency_overrides[get_budget_service] = lambda: budget
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        get_response = await client.get("/api/admin/budget/config")
        assert get_response.status_code == 200
        assert get_response.json()["mode"] == "observe"

        patch_response = await client.patch(
            "/api/admin/budget/config",
            json={"per_task_limit": 2.5, "per_batch_limit": None, "mode": "warn"},
        )
        assert patch_response.status_code == 200
        assert patch_response.json()["per_task_limit"] == 2.5
        assert patch_response.json()["mode"] == "warn"

        invalid = await client.patch(
            "/api/admin/budget/config",
            json={"per_task_limit": None, "per_batch_limit": None, "mode": "bogus"},
        )
        assert invalid.status_code == 422


async def test_budget_usage_api(sessions) -> None:
    budget = await _configure(sessions, "observe")
    app = FastAPI()
    app.include_router(audit_budget_router, prefix="/api")
    app.dependency_overrides[get_budget_service] = lambda: budget
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/budget/usage", params={"project_id": "p-1"})
        assert response.status_code == 200
        body = response.json()
        assert body["project_id"] == "p-1"
        assert body["job_count"] == 0
