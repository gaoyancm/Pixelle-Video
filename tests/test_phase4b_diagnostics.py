"""Phase 04-B Q4 diagnostic engine tests."""

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
from pixelle_video.qc.diagnostics import QCDiagnosticEngine
from pixelle_video.qc.executor import QCExecutor
from pixelle_video.qc.repository import QCRepository


def test_engine_has_at_least_eight_builtin_diagnoses() -> None:
    engine = QCDiagnosticEngine()
    assert len(engine.known_issue_types()) >= 8
    assert {
        "resolution_mismatch",
        "frame_rate_mismatch",
        "file_size_abnormal",
        "audio_missing",
        "frame_instability",
        "character_drift",
        "subtitle_break_error",
        "brand_color_deviation",
    } <= set(engine.known_issue_types())


def test_diagnose_returns_symptom_root_cause_fix() -> None:
    engine = QCDiagnosticEngine()
    diagnosis = engine.diagnose("resolution_mismatch")
    assert diagnosis.symptom
    assert diagnosis.root_cause
    assert diagnosis.fix
    assert "workflow" in diagnosis.fix


def test_diagnose_unknown_type_returns_fallback() -> None:
    engine = QCDiagnosticEngine()
    diagnosis = engine.diagnose("no_such_issue")
    assert diagnosis.issue_type == "no_such_issue"
    assert diagnosis.symptom == "未知问题类型"


def test_diagnose_accepts_context() -> None:
    engine = QCDiagnosticEngine()
    diagnosis = engine.diagnose("brand_color_deviation", {"brand_hex": "#FF0000"})
    assert diagnosis.issue_type == "brand_color_deviation"
    assert diagnosis.fix


def test_diagnose_issues_batch() -> None:
    engine = QCDiagnosticEngine()
    payloads = engine.diagnose_issues(["audio_missing", "character_drift"])
    assert len(payloads) == 2
    assert payloads[0]["issue_type"] == "audio_missing"
    assert payloads[0]["fix"]


@pytest.fixture
async def api_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'diag.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = QCRepository(factory)
    service = QCApplicationService(repository, QCExecutor(repository))
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def test_api_diagnose(api_env) -> None:
    _factory, _repository, service = api_env
    app = FastAPI()
    app.include_router(qc_router, prefix="/api")
    app.dependency_overrides[get_qc_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/admin/qc/diagnose",
            json={"issue_type": "frame_instability", "context": {}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["issue_type"] == "frame_instability"
    assert body["root_cause"]
    assert body["fix"]
