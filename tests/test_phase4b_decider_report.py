"""Phase 04-B Q3 decision and report tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_qc_service
from api.routers.qc import router as qc_router
from api.services.qc import QCApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.qc.decider import QCDecider
from pixelle_video.qc.executor import QCExecutor
from pixelle_video.qc.repository import QCRepository
from pixelle_video.qc.types import QCIssue, QCResult


def _result(*issues: QCIssue) -> QCResult:
    return QCResult(
        job_id="job-1",
        executed_at=datetime.now(timezone.utc),
        profile="default",
        total_rules=3,
        issues=list(issues),
    )


def _issue(severity: str, field: str = "resolution") -> QCIssue:
    return QCIssue(
        rule_id="r1",
        severity=severity,
        category="technical",
        field=field,
        expected="x",
        actual="y",
        message="未通过",
    )


def test_decide_pass_when_no_issues() -> None:
    decision = QCDecider().decide(_result())
    assert decision.decision == "pass"


def test_decide_pass_with_warnings_for_minor_only() -> None:
    decision = QCDecider().decide(_result(_issue("minor")))
    assert decision.decision == "pass_with_warnings"


def test_decide_human_review_for_single_major() -> None:
    decision = QCDecider().decide(_result(_issue("major")))
    assert decision.decision == "human_review"


def test_decide_partial_redo_for_single_critical() -> None:
    decision = QCDecider().decide(_result(_issue("critical")))
    assert decision.decision == "partial_redo"


def test_decide_full_reject_for_multiple_critical() -> None:
    decision = QCDecider().decide(_result(_issue("critical"), _issue("critical", "frame_rate")))
    assert decision.decision == "full_reject"


def test_decide_full_reject_for_multiple_major() -> None:
    decision = QCDecider().decide(_result(_issue("major"), _issue("major", "frame_rate")))
    assert decision.decision == "full_reject"


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'report.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = QCRepository(factory)
    job_repository = MediaJobRepository(factory)
    audit = AuditRepository(factory)
    executor = QCExecutor(
        repository,
        job_lookup=job_repository.get_job,
        output_path_resolver=lambda job_id: None,
        evidence_provider=lambda job: {
            "resolution": "640x360",
            "frame_rate": "25",
            "audio_channels": "2",
            "size_bytes": "12345",
        },
    )
    service = QCApplicationService(repository, executor, audit=audit)
    try:
        yield factory, repository, job_repository, service
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


async def _seed_resolution_rule(repository: QCRepository) -> None:
    await repository.create_rule(
        name="分辨率检查",
        category="technical",
        rule_type="schema_validation",
        rule_config_json={
            "field": "resolution",
            "operator": "min",
            "expected": "1920x1080",
            "severity": "major",
        },
        priority=1,
        rule_id="r-resolution",
    )
    await repository.create_profile(
        name="default",
        rules_json=[{"rule_id": "r-resolution", "severity_override": None}],
        is_default=1,
    )


async def test_run_qc_writes_audit_event(env) -> None:
    _factory, repository, job_repository, service = env
    await _seed_resolution_rule(repository)
    job = (await job_repository.create_job(_job_create())).job
    payload = await service.run_qc(job.job_id)
    assert payload["decision"] == "human_review"
    events, _ = await service.audit.list(
        scope_type="job", scope_id=job.job_id, event_type="qc_completed"
    )
    assert len(events) == 1
    assert events[0].details_json["decision"] == "human_review"


async def test_get_result_reads_latest_audit_event(env) -> None:
    _factory, repository, job_repository, service = env
    await _seed_resolution_rule(repository)
    job = (await job_repository.create_job(_job_create())).job
    await service.run_qc(job.job_id)
    result = await service.get_result(job.job_id)
    assert result is not None
    assert result["decision"] == "human_review"
    assert result["job_id"] == job.job_id
    assert result["executed_at"] is not None
    assert result["suggestions"] is not None


async def test_run_qc_payload_shape(env) -> None:
    _factory, repository, job_repository, service = env
    await _seed_resolution_rule(repository)
    job = (await job_repository.create_job(_job_create())).job
    payload = await service.run_qc(job.job_id)
    assert set(payload) == {
        "job_id",
        "executed_at",
        "profile",
        "total_rules",
        "passed",
        "issues",
        "decision",
        "suggestions",
    }
    assert payload["total_rules"] == 1 and payload["passed"] == 0


@pytest.fixture
async def api_client(env):
    _factory, _repository, _job_repository, service = env
    app = FastAPI()
    app.include_router(qc_router, prefix="/api")
    app.dependency_overrides[get_qc_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, _job_repository


async def test_api_run_and_report(api_client) -> None:
    client, _factory, repository, job_repository = api_client
    await _seed_resolution_rule(repository)
    job = (await job_repository.create_job(_job_create())).job
    run = await client.post(f"/api/admin/qc/run/{job.job_id}")
    assert run.status_code == 200
    body = run.json()
    assert body["decision"] in {
        "pass",
        "pass_with_warnings",
        "human_review",
        "partial_redo",
        "full_reject",
    }
    assert isinstance(body["issues"], list)

    report = await client.get(f"/api/admin/qc/report/{job.job_id}")
    assert report.status_code == 200
    report_body = report.json()
    assert report_body["job_id"] == job.job_id
    assert "report_text" in report_body
    assert "诊断" in report_body["report_text"]


async def test_api_results_after_run(api_client) -> None:
    client, _factory, repository, job_repository = api_client
    await _seed_resolution_rule(repository)
    job = (await job_repository.create_job(_job_create())).job
    await client.post(f"/api/admin/qc/run/{job.job_id}")
    results = await client.get(f"/api/admin/qc/results/{job.job_id}")
    assert results.status_code == 200
    assert results.json()["job_id"] == job.job_id


async def test_api_results_not_found(api_client) -> None:
    client, _factory, repository, job_repository = api_client
    job = (await job_repository.create_job(_job_create())).job
    results = await client.get(f"/api/admin/qc/results/{job.job_id}")
    assert results.status_code == 404
