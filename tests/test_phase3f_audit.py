"""Phase 03-F F1 audit trail tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_audit_service
from api.routers.audit_budget import router as audit_router
from pixelle_video.audit import AuditRepository
from pixelle_video.management import ManagementRepository
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus


@pytest.fixture
async def sessions(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'audit.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _job_create() -> MediaJobCreate:
    return MediaJobCreate(
        workflow_type="a800_wan22_t2v_33f",
        workflow_key="workflow.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id="a800",
        input_json={"prompt": "safe prompt"},
        input_assets_json=[],
        idempotency_key=None,
    )


async def test_audit_repository_records_and_reads(sessions) -> None:
    audit = AuditRepository(sessions)
    event = await audit.record(
        event_type="job_created",
        scope_type="job",
        scope_id="job-1",
        details={"workflow_type": "a800_wan22_t2v_33f"},
        cost_snapshot={"estimated_cost": 1.0},
    )
    assert event.event_id
    fetched = await audit.get(event.event_id)
    assert fetched is not None
    assert fetched.event_type == "job_created"
    assert fetched.details_json["workflow_type"] == "a800_wan22_t2v_33f"
    assert fetched.cost_snapshot["estimated_cost"] == 1.0
    assert fetched.operator == "system"


async def test_job_creation_produces_audit_event(sessions) -> None:
    audit = AuditRepository(sessions)
    repository = MediaJobRepository(sessions, audit=audit)
    result = await repository.create_job(_job_create())
    assert result.created is True
    events = await audit.list(scope_type="job", scope_id=result.job.job_id)
    assert len(events[0]) == 1
    assert events[0][0].event_type == "job_created"


async def test_job_status_change_produces_audit_event(sessions) -> None:
    audit = AuditRepository(sessions)
    repository = MediaJobRepository(sessions, audit=audit)
    job = (await repository.create_job(_job_create())).job
    await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    running = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version + 1,
        target_status=JobStatus.RUNNING,
    )
    events, _ = await audit.list(scope_type="job", scope_id=job.job_id)
    types = {event.event_type for event in events}
    assert "job_created" in types
    assert "job_status_changed" in types
    changed = [event for event in events if event.event_type == "job_status_changed"]
    assert {event.details_json["to"] for event in changed} == {"submitting", "running"}
    assert running.status == JobStatus.RUNNING.value


async def test_management_operation_produces_audit_with_cost_snapshot(sessions) -> None:
    audit = AuditRepository(sessions)
    management = ManagementRepository(sessions, audit=audit)
    await management.create_operation(
        scope_type="batch",
        scope_id="batch-1",
        operation_type="batch_submit",
        idempotency_key="key-1",
        request_hash="a" * 64,
        result_json={"batch_id": "batch-1"},
        cost_snapshot={"operation_id": "op-1"},
    )
    events, _ = await audit.list(scope_type="batch", scope_id="batch-1")
    assert len(events) == 1
    assert events[0].event_type == "operation_performed"
    assert events[0].cost_snapshot == {"operation_id": "op-1"}


async def test_audit_list_pagination_is_stable(sessions) -> None:
    audit = AuditRepository(sessions)
    for index in range(5):
        await audit.record(event_type="event", scope_type="job", scope_id=f"job-{index}")
    page1, has_more = await audit.list(limit=3, offset=0)
    assert len(page1) == 3 and has_more is True
    page2, has_more2 = await audit.list(limit=3, offset=3)
    assert len(page2) == 2 and has_more2 is False
    seen = {event.event_id for event in page1 + page2}
    assert len(seen) == 5


async def test_audit_query_by_scope_and_event_type(sessions) -> None:
    audit = AuditRepository(sessions)
    await audit.record(event_type="job_created", scope_type="job", scope_id="job-a")
    await audit.record(event_type="job_status_changed", scope_type="job", scope_id="job-a")
    await audit.record(event_type="job_created", scope_type="job", scope_id="job-b")
    only_created, _ = await audit.list(scope_type="job", scope_id="job-a", event_type="job_created")
    assert [event.event_type for event in only_created] == ["job_created"]


@pytest.fixture
async def api_client(sessions):
    audit = AuditRepository(sessions)
    for index in range(3):
        await audit.record(
            event_type="job_created",
            scope_type="job",
            scope_id=f"job-{index}",
            details={"index": index},
        )
    app = FastAPI()
    app.include_router(audit_router, prefix="/api")
    app.dependency_overrides[get_audit_service] = lambda: audit
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_audit_api_lists_and_filters(api_client) -> None:
    response = await api_client.get("/api/admin/audit", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2 and body["has_more"] is True
    assert body["items"][0]["event_type"] == "job_created"

    filtered = await api_client.get(
        "/api/admin/audit", params={"scope_type": "job", "scope_id": "job-0"}
    )
    assert filtered.status_code == 200
    assert [item["scope_id"] for item in filtered.json()["items"]] == ["job-0"]


async def test_audit_api_requires_scope_pair(api_client) -> None:
    response = await api_client.get("/api/admin/audit", params={"scope_type": "job"})
    assert response.status_code == 422


async def test_audit_api_get_single_event(api_client) -> None:
    listing = await api_client.get("/api/admin/audit", params={"limit": 1})
    event_id = listing.json()["items"][0]["event_id"]
    response = await api_client.get(f"/api/admin/audit/{event_id}")
    assert response.status_code == 200
    assert response.json()["event_id"] == event_id
    missing = await api_client.get("/api/admin/audit/does-not-exist")
    assert missing.status_code == 404
