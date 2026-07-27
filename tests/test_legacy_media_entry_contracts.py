from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from api.dependencies import get_media_job_service, get_pixelle_video
from api.routers import tasks as tasks_router
from api.routers import video as video_router
from api.routers.media_jobs import router as media_jobs_router
from api.schemas.video import VideoGenerateRequest
from api.tasks.models import Task, TaskStatus, TaskType
from pixelle_video.media_assets import AssetNotFoundError, AssetUnavailableError


def legacy_video_request() -> VideoGenerateRequest:
    return VideoGenerateRequest(
        text="keep the complete composite pipeline",
        frame_template="1080x1920/image_default.html",
    )


def public_client(router, *, dependency_overrides=None):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    for dependency, replacement in (dependency_overrides or {}).items():
        app.dependency_overrides[dependency] = replacement
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_legacy_sync_public_route_keeps_synchronous_success_contract(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "legacy.mp4"
    output.write_bytes(b"legacy-video")
    calls = []

    class FakeCore:
        async def generate_video(self, **parameters):
            calls.append(parameters)
            return SimpleNamespace(video_path=str(output), duration=2.5)

    monkeypatch.setattr(
        video_router.task_manager,
        "create_task",
        lambda **kwargs: pytest.fail("sync route must not create a legacy task"),
    )
    async with public_client(
        video_router.router,
        dependency_overrides={get_pixelle_video: lambda: FakeCore()},
    ) as client:
        response = await client.post(
            "/api/video/generate/sync",
            json=legacy_video_request().model_dump(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "Success",
        "video_url": "http://test/api/files/legacy.mp4",
        "duration": 2.5,
        "file_size": len(b"legacy-video"),
    }
    assert len(calls) == 1
    assert "task_id" not in response.json()
    assert "job_id" not in response.json()


@pytest.mark.asyncio
async def test_legacy_sync_provider_failure_stays_on_original_public_route(monkeypatch):
    calls = []

    class FailingCore:
        async def generate_video(self, **parameters):
            calls.append(parameters)
            raise RuntimeError("provider submission failed")

    monkeypatch.setattr(
        video_router.task_manager,
        "create_task",
        lambda **kwargs: pytest.fail("provider failure must not create a second task"),
    )
    async with public_client(
        video_router.router,
        dependency_overrides={get_pixelle_video: lambda: FailingCore()},
    ) as client:
        response = await client.post(
            "/api/video/generate/sync",
            json=legacy_video_request().model_dump(),
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "provider submission failed"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_legacy_async_keeps_task_id_shape_and_single_task_manager_submission(
    monkeypatch,
):
    created = []
    executed = []

    def create_task(*, task_type, request_params):
        created.append((task_type, request_params))
        return SimpleNamespace(task_id="legacy-task-id")

    async def execute_task(*, task_id, coro_func):
        executed.append((task_id, coro_func))

    monkeypatch.setattr(video_router.task_manager, "create_task", create_task)
    monkeypatch.setattr(video_router.task_manager, "execute_task", execute_task)
    request = Request({"type": "http", "scheme": "http", "server": ("test", 80), "path": "/"})

    response = await video_router.generate_video_async(
        legacy_video_request(),
        SimpleNamespace(),
        request,
    )

    assert response.model_dump() == {
        "success": True,
        "message": "Task created successfully",
        "task_id": "legacy-task-id",
    }
    assert created[0][0] is TaskType.VIDEO_GENERATION
    assert len(created) == len(executed) == 1


@pytest.mark.asyncio
async def test_legacy_task_query_keeps_in_memory_task_shape(monkeypatch):
    task = SimpleNamespace(
        task_id="legacy-task-id",
        task_type=TaskType.VIDEO_GENERATION,
        status=TaskStatus.RUNNING,
        progress=None,
        result=None,
        error=None,
        created_at=None,
        started_at=None,
        completed_at=None,
        request_params={"text": "legacy"},
    )
    monkeypatch.setattr(tasks_router.task_manager, "get_task", lambda task_id: task)
    assert await tasks_router.get_task("legacy-task-id") is task


@pytest.mark.asyncio
async def test_legacy_task_query_not_found_contract(monkeypatch):
    monkeypatch.setattr(tasks_router.task_manager, "get_task", lambda task_id: None)
    with pytest.raises(HTTPException) as caught:
        await tasks_router.get_task("missing")
    assert caught.value.status_code == 404
    assert caught.value.detail == "Task missing not found"


@pytest.mark.asyncio
async def test_legacy_cancel_contract_and_not_found(monkeypatch):
    monkeypatch.setattr(tasks_router.task_manager, "cancel_task", lambda task_id: True)
    assert await tasks_router.cancel_task("legacy-task-id") == {
        "success": True,
        "message": "Task legacy-task-id cancelled successfully",
    }

    monkeypatch.setattr(tasks_router.task_manager, "cancel_task", lambda task_id: False)
    with pytest.raises(HTTPException) as caught:
        await tasks_router.cancel_task("missing")
    assert caught.value.status_code == 404
    assert caught.value.detail == "Task missing not found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [
            Task(
                task_id="legacy-list-id",
                task_type=TaskType.VIDEO_GENERATION,
                status=TaskStatus.RUNNING,
                created_at=datetime(2026, 7, 26, 12, 0, 0),
                request_params={"text": "legacy"},
            )
        ],
    ],
)
async def test_legacy_task_list_public_contract_uses_task_manager(monkeypatch, tasks):
    calls = []

    def list_tasks(*, status, limit):
        calls.append((status, limit))
        return tasks

    monkeypatch.setattr(tasks_router.task_manager, "list_tasks", list_tasks)
    async with public_client(tasks_router.router) as client:
        response = await client.get("/api/tasks", params={"limit": 25})

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert calls == [(None, 25)]
    if tasks:
        assert response.json()[0]["task_id"] == "legacy-list-id"
        assert response.json()[0]["status"] == "running"
        assert response.json()[0]["task_type"] == "video_generation"
        assert "job_id" not in response.json()[0]
    else:
        assert response.json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AssetNotFoundError(), "invalid_asset"),
        (AssetUnavailableError(), "invalid_asset"),
    ],
)
async def test_input_asset_error_public_contract_is_stable_and_submits_nothing(
    error,
    expected_code,
):
    calls = []

    class RejectingService:
        async def create(self, request, idempotency_key):
            calls.append((request, idempotency_key))
            raise error

    async with public_client(
        media_jobs_router,
        dependency_overrides={get_media_job_service: lambda: RejectingService()},
    ) as client:
        response = await client.post(
            "/api/media/jobs",
            headers={"Idempotency-Key": "asset-validation"},
            json={
                "workflow": "gpu_4090_wan21_i2v_33f",
                "asset_id": "missing-or-unavailable",
                "parameters": {"prompt": "animate"},
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": expected_code,
            "message": "The input asset is invalid or unavailable.",
        }
    }
    assert len(calls) == 1
    assert "D:\\" not in response.text
    assert "Traceback" not in response.text
    assert "provider" not in response.text.lower()
