from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import api.routers.media_jobs as media_jobs_router
from api.dependencies import get_media_job_service
from api.schemas.media_jobs import MediaJobRequest
from pixelle_video.media_migration import CompatibilitySubmissionFacade, MigrationRoute
from pixelle_video.media_migration.facade import classify_submission


class RecordingService:
    def __init__(self, *, failure: Exception | None = None):
        self.calls = []
        self.failure = failure

    async def create(self, request, idempotency_key):
        self.calls.append((request, idempotency_key))
        if self.failure is not None:
            raise self.failure
        now = datetime.now(timezone.utc)
        return (
            SimpleNamespace(
                job_id=f"sqlite-{request.workflow}",
                workflow_type=request.workflow,
                status="queued",
                created_at=now,
                updated_at=now,
                cancel_requested_at=None,
                retry_of_job_id=None,
                output_metadata=[],
                error_category=None,
            ),
            True,
        )


def public_client(service):
    app = FastAPI()
    app.include_router(media_jobs_router.router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.parametrize(
    ("workflow", "asset_id"),
    [
        ("a800_wan22_t2v_33f", None),
        ("a800_wan22_t2v_81f", None),
        ("gpu_4090_wan21_i2v_33f", "asset-1"),
        ("gpu_4090_wan21_i2v_81f", "asset-1"),
    ],
)
async def test_public_create_uses_facade_and_one_persistent_service_call(
    monkeypatch,
    workflow,
    asset_id,
):
    decisions = []
    legacy_calls = []
    service = RecordingService()

    class RecordingFacade(CompatibilitySubmissionFacade):
        async def submit(self, request):
            decisions.append(classify_submission(request).route)
            return await super().submit(request)

    async def forbidden_legacy(submission):
        legacy_calls.append(submission)
        pytest.fail("the explicit persistent endpoint must not invoke the legacy submitter")

    monkeypatch.setattr(media_jobs_router, "CompatibilitySubmissionFacade", RecordingFacade)
    monkeypatch.setattr(
        media_jobs_router,
        "_reject_legacy_composite_submission",
        forbidden_legacy,
    )
    body = {"workflow": workflow, "parameters": {"prompt": "safe prompt"}}
    if asset_id is not None:
        body["asset_id"] = asset_id

    async with public_client(service) as client:
        response = await client.post(
            "/api/media/jobs",
            json=body,
            headers={"Idempotency-Key": f"key-{workflow}"},
        )

    assert response.status_code == 201
    assert decisions == [MigrationRoute.PERSISTENT_LEAF]
    assert len(service.calls) == 1
    submitted_request, submitted_key = service.calls[0]
    assert isinstance(submitted_request, MediaJobRequest)
    assert submitted_request.model_dump() == MediaJobRequest.model_validate(body).model_dump()
    assert submitted_key == f"key-{workflow}"
    assert legacy_calls == []
    payload = response.json()
    assert set(payload) == {
        "job_id",
        "workflow",
        "status",
        "created_at",
        "updated_at",
        "cancel_requested",
        "retry_of_job_id",
        "outputs",
        "error",
    }
    assert payload["job_id"] == f"sqlite-{workflow}"
    assert payload["workflow"] == workflow
    assert payload["status"] == "queued"
    assert "task_id" not in payload
    assert "provider" not in payload


async def test_persistent_failure_is_redacted_without_legacy_fallback(monkeypatch):
    legacy_calls = []
    service = RecordingService(failure=RuntimeError("provider secret D:\\private"))

    async def forbidden_legacy(submission):
        legacy_calls.append(submission)
        pytest.fail("persistent failure must not fall back to the legacy submitter")

    monkeypatch.setattr(
        media_jobs_router,
        "_reject_legacy_composite_submission",
        forbidden_legacy,
    )
    async with public_client(service) as client:
        response = await client.post(
            "/api/media/jobs",
            json={
                "workflow": "a800_wan22_t2v_33f",
                "parameters": {"prompt": "safe prompt"},
            },
            headers={"Idempotency-Key": "failure-key"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }
    assert len(service.calls) == 1
    assert legacy_calls == []
    assert "secret" not in response.text
    assert "private" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"workflow": "unknown", "parameters": {"prompt": "safe prompt"}},
        {"workflow": "a800_wan22_t2v_33f", "parameters": {}},
        {
            "workflow": "a800_wan22_t2v_33f",
            "parameters": {"prompt": "safe prompt", "width": 20_000},
        },
        {
            "workflow": "a800_wan22_t2v_33f",
            "parameters": {"prompt": "safe prompt"},
            "asset_id": "asset-1",
        },
        {
            "workflow": "gpu_4090_wan21_i2v_33f",
            "parameters": {"prompt": "safe prompt"},
        },
        {
            "workflow": "gpu_4090_wan21_i2v_33f",
            "parameters": {"prompt": "safe prompt"},
            "asset_ids": ["asset-1", "asset-2"],
        },
        {
            "workflow": "gpu_4090_wan21_i2v_33f",
            "parameters": {"prompt": "safe prompt"},
            "image_path": "D:\\raw\\input.png",
        },
    ],
)
async def test_invalid_public_requests_call_neither_facade_submitter(monkeypatch, body):
    service = RecordingService()

    class ForbiddenFacade:
        def __init__(self, **_kwargs):
            pytest.fail("schema-invalid requests must not instantiate the Facade")

    monkeypatch.setattr(media_jobs_router, "CompatibilitySubmissionFacade", ForbiddenFacade)
    async with public_client(service) as client:
        response = await client.post(
            "/api/media/jobs",
            json=body,
            headers={"Idempotency-Key": "invalid-key"},
        )

    assert response.status_code == 422
    assert service.calls == []
