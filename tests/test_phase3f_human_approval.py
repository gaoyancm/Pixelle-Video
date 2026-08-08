"""Phase 03-F F2 human approval state tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_media_job_service
from api.routers.media_jobs import router as media_jobs_router
from api.services.media_jobs import MediaJobApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.config.schema import MediaJobsConfig
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base, MediaJob
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidStateTransition,
    JobStatus,
    can_cancel,
    is_terminal,
    validate_transition,
)

# --- Pure state machine tests ----------------------------------------------


def test_awaiting_human_is_defined_in_status_enum() -> None:
    assert JobStatus.AWAITING_HUMAN == "awaiting_human"


def test_legal_transitions_include_awaiting_human() -> None:
    assert JobStatus.AWAITING_HUMAN in LEGAL_TRANSITIONS[JobStatus.RUNNING]
    assert {
        JobStatus.RUNNING,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
    } == LEGAL_TRANSITIONS[JobStatus.AWAITING_HUMAN]


def test_awaiting_human_is_not_terminal() -> None:
    assert is_terminal(JobStatus.AWAITING_HUMAN) is False
    assert JobStatus.AWAITING_HUMAN not in TERMINAL_STATUSES


def test_awaiting_human_can_be_cancelled() -> None:
    assert can_cancel(JobStatus.AWAITING_HUMAN) is True


def test_queued_to_awaiting_human_is_illegal() -> None:
    with pytest.raises(InvalidStateTransition):
        validate_transition(JobStatus.QUEUED, JobStatus.AWAITING_HUMAN)


def test_existing_seven_status_transitions_unchanged() -> None:
    # Regression: every pre-existing transition must still be legal.
    assert JobStatus.SUCCEEDED in LEGAL_TRANSITIONS[JobStatus.RUNNING]
    assert JobStatus.FAILED in LEGAL_TRANSITIONS[JobStatus.QUEUED]
    assert JobStatus.CANCELLED in LEGAL_TRANSITIONS[JobStatus.RUNNING]
    assert JobStatus.TIMED_OUT in LEGAL_TRANSITIONS[JobStatus.RUNNING]
    for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT):
        assert LEGAL_TRANSITIONS[status] == frozenset()


# --- Repository / API tests -------------------------------------------------


def node_id_for_workflow(workflow: str) -> str:
    return "a800"


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


async def _make_running(sessions, repository: MediaJobRepository) -> MediaJob:
    job = (await repository.create_job(_job_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    return await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )


@pytest.fixture
async def sessions(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'approval.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_request_approval_suspends_running_job(sessions) -> None:
    repository = MediaJobRepository(sessions, audit=AuditRepository(sessions))
    job = await _make_running(sessions, repository)
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for_workflow,
        audit=AuditRepository(sessions),
    )
    suspended = await service.request_approval(job.job_id)
    assert suspended.status == JobStatus.AWAITING_HUMAN.value
    events, _ = await AuditRepository(sessions).list(scope_type="job", scope_id=job.job_id)
    assert any(event.event_type == "approval_requested" for event in events)


async def test_approve_resumes_awaiting_human_job(sessions) -> None:
    repository = MediaJobRepository(sessions, audit=AuditRepository(sessions))
    job = await _make_running(sessions, repository)
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for_workflow,
        audit=AuditRepository(sessions),
    )
    await service.request_approval(job.job_id)
    resumed = await service.approve(job.job_id)
    assert resumed.status == JobStatus.RUNNING.value


async def test_reject_cancels_and_records_audit_reason(sessions) -> None:
    audit = AuditRepository(sessions)
    repository = MediaJobRepository(sessions, audit=audit)
    job = await _make_running(sessions, repository)
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for_workflow,
        audit=audit,
    )
    await service.request_approval(job.job_id)
    rejected = await service.reject(job.job_id, reason="brand violation")
    assert rejected.status == JobStatus.CANCELLED.value
    events, _ = await audit.list(scope_type="job", scope_id=job.job_id)
    rejection = next(event for event in events if event.event_type == "approval_rejected")
    assert rejection.details_json["reason"] == "brand violation"


async def test_approve_on_non_awaiting_job_raises(sessions) -> None:
    repository = MediaJobRepository(sessions)
    job = await _make_running(sessions, repository)
    service = MediaJobApplicationService(
        repository, MediaJobsConfig(enabled=True), node_selector=node_id_for_workflow
    )
    from api.services.media_jobs import JobNotApprovalableError

    with pytest.raises(JobNotApprovalableError):
        await service.approve(job.job_id)


async def test_worker_claim_excludes_awaiting_human(sessions) -> None:
    repository = MediaJobRepository(sessions, audit=AuditRepository(sessions))
    job = await _make_running(sessions, repository)
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for_workflow,
        audit=AuditRepository(sessions),
    )
    await service.request_approval(job.job_id)
    candidates = await repository.list_claim_candidates(
        now=datetime.now(timezone.utc),
        statuses=(JobStatus.QUEUED, JobStatus.SUBMITTING, JobStatus.RUNNING),
    )
    assert all(candidate.job_id != job.job_id for candidate in candidates)


@pytest.fixture
async def api_client(sessions):
    repository = MediaJobRepository(sessions, audit=AuditRepository(sessions))
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for_workflow,
        audit=AuditRepository(sessions),
    )
    app = FastAPI()
    app.include_router(media_jobs_router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, service, sessions


async def test_request_approval_api_endpoint(api_client) -> None:
    client, service, sessions = api_client
    job = await _make_running(sessions, service.repository)
    response = await client.post(f"/api/media/jobs/{job.job_id}/request-approval")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_human"


async def test_approve_api_endpoint(api_client) -> None:
    client, service, sessions = api_client
    job = await _make_running(sessions, service.repository)
    await client.post(f"/api/media/jobs/{job.job_id}/request-approval")
    response = await client.post(f"/api/media/jobs/{job.job_id}/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "running"


async def test_reject_api_endpoint(api_client) -> None:
    client, service, sessions = api_client
    job = await _make_running(sessions, service.repository)
    await client.post(f"/api/media/jobs/{job.job_id}/request-approval")
    response = await client.post(
        f"/api/media/jobs/{job.job_id}/reject", json={"reason": "not on brand"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_approval_api_rejects_wrong_state(api_client) -> None:
    client, service, sessions = api_client
    job = await _make_running(sessions, service.repository)
    response = await client.post(f"/api/media/jobs/{job.job_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_approvalable"
