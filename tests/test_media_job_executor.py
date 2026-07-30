from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.dependencies import get_media_job_service
from api.routers.media_jobs import router as media_jobs_router
from api.services.media_jobs import MediaJobApplicationService
from pixelle_video.config.schema import MediaJobsConfig
from pixelle_video.media_jobs.contracts import MediaInputAsset, MediaJobCreate
from pixelle_video.media_jobs.database import create_media_jobs_engine, sqlite_url_for_path
from pixelle_video.media_jobs.executor import RecoverableComfyUIExecutor
from pixelle_video.media_jobs.models import Base, utc_now
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import (
    ErrorCategory,
    JobStatus,
    RemoteJobStatus,
)
from pixelle_video.media_jobs.worker import MediaJobWorker
from pixelle_video.services.comfyui_adapter import ComfyUIAdapter
from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS

PROJECT_ROOT = Path(__file__).parents[1]
WORKFLOW_ROOT = PROJECT_ROOT / "workflows" / "selfhost"
WORKFLOW_TYPES = tuple(WORKFLOW_SPECS)


async def open_repository(
    database_path: Path,
) -> tuple[MediaJobRepository, object]:
    engine = create_media_jobs_engine(sqlite_url_for_path(database_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return MediaJobRepository(factory), engine


def nodes() -> list[dict]:
    return [
        {
            "id": "a800",
            "name": "A800",
            "base_url": "http://mock-comfyui",
            "workflow_types": [
                "a800_wan22_t2v_33f",
                "a800_wan22_t2v_81f",
            ],
            "enabled": True,
            "timeout_seconds": 10,
            "concurrency": 1,
        },
        {
            "id": "gpu-4090",
            "name": "4090",
            "base_url": "http://mock-comfyui",
            "workflow_types": [
                "gpu_4090_wan21_i2v_33f",
                "gpu_4090_wan21_i2v_81f",
            ],
            "enabled": True,
            "timeout_seconds": 10,
            "concurrency": 1,
        },
    ]


def node_id_for(workflow_type: str) -> str:
    return "gpu-4090" if WORKFLOW_SPECS[workflow_type].requires_image else "a800"


def make_create(
    workflow_type: str,
    *,
    asset_id: str | None = None,
    deadline_offset: timedelta = timedelta(minutes=5),
) -> MediaJobCreate:
    spec = WORKFLOW_SPECS[workflow_type]
    return MediaJobCreate(
        workflow_type=workflow_type,
        workflow_key=spec.workflow_key,
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id=node_id_for(workflow_type),
        input_json={
            "prompt": f"test {workflow_type}",
            "negative_prompt": "bad",
            "width": 512,
            "height": 512,
            "frame_count": 81 if workflow_type.endswith("81f") else 33,
            "seed": 42,
            "steps": 20 if spec.requires_image else None,
            "cfg": 6 if spec.requires_image else None,
        },
        input_assets_json=(
            [MediaInputAsset(asset_id=asset_id, role="first_frame")]
            if asset_id
            else []
        ),
        deadline_at=utc_now() + deadline_offset,
    )


async def create_workflow_job(
    repository: MediaJobRepository,
    asset_root: Path,
    workflow_type: str,
):
    asset_id = None
    if WORKFLOW_SPECS[workflow_type].requires_image:
        asset_id = f"{workflow_type}.jpg"
        asset_root.mkdir(parents=True, exist_ok=True)
        (asset_root / asset_id).write_bytes(b"fake-image")
    return (
        await repository.create_job(
            make_create(workflow_type, asset_id=asset_id)
        )
    ).job


def make_worker(
    repository: MediaJobRepository,
    tmp_path: Path,
    handler,
    *,
    worker_id: str = "executor-worker",
    history_poll_interval_seconds: float = 0.001,
) -> MediaJobWorker:
    adapter = ComfyUIAdapter(
        nodes(),
        workflow_root=WORKFLOW_ROOT,
        transport=httpx.MockTransport(handler),
    )
    executor = RecoverableComfyUIExecutor(
        repository,
        adapter,
        managed_asset_root=tmp_path / "assets",
        managed_output_root=tmp_path / "outputs",
        history_poll_interval_seconds=history_poll_interval_seconds,
    )
    return MediaJobWorker(
        repository,
        executor,
        worker_id=worker_id,
        lease_seconds=2,
        heartbeat_seconds=0.1,
    )


@pytest.mark.parametrize("workflow_type", WORKFLOW_TYPES)
@pytest.mark.asyncio
async def test_each_workflow_submits_after_marker_and_persists_valid_output(
    tmp_path: Path,
    workflow_type: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"{workflow_type}.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)
    submitted: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.jpg", "subfolder": "inputs"})
        if request.url.path == "/prompt":
            stored = await repository.get_job(job.job_id)
            assert stored.status == JobStatus.SUBMITTING.value
            assert stored.submit_started_at is not None
            assert stored.comfyui_prompt_id is None
            payload = json.loads(request.content)
            submitted.append(payload)
            return httpx.Response(200, json={"prompt_id": f"prompt-{workflow_type}"})
        if request.url.path == f"/history/prompt-{workflow_type}":
            persisted = await repository.get_job(job.job_id)
            assert persisted.comfyui_prompt_id == f"prompt-{workflow_type}"
            assert persisted.status == JobStatus.RUNNING.value
            return httpx.Response(
                200,
                json={
                    f"prompt-{workflow_type}": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "output": {
                                "images": [
                                    {
                                        "filename": f"{workflow_type}.webm",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=f"video-{workflow_type}".encode())
        return httpx.Response(200, json={"queue_running": [], "queue_pending": []})

    worker = make_worker(repository, tmp_path, handler)
    assert await worker.run_once() == 1

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUCCEEDED.value
    assert stored.comfyui_prompt_id == f"prompt-{workflow_type}"
    assert stored.output_metadata[0]["relative_path"].endswith(f"{workflow_type}.webm")
    assert stored.lease_owner is None
    assert len(submitted) == 1
    assert submitted[0]["client_id"] == job.submission_token
    assert submitted[0]["extra_data"] == {
        "pixelle_submission_token": job.submission_token
    }
    if WORKFLOW_SPECS[workflow_type].requires_image:
        assert submitted[0]["prompt"]["52"]["inputs"]["image"] == "inputs/uploaded.jpg"
    else:
        assert submitted[0]["prompt"]["89"]["inputs"]["text"] == f"test {workflow_type}"
    await engine.dispose()


@pytest.mark.parametrize(
    ("workflow_type", "asset_id"),
    [
        ("a800_wan22_t2v_33f", None),
        ("gpu_4090_wan21_i2v_33f", "input.jpg"),
    ],
)
@pytest.mark.asyncio
async def test_public_api_job_hands_off_to_real_worker_with_one_mock_submission(
    tmp_path: Path,
    workflow_type: str,
    asset_id: str | None,
) -> None:
    repository, engine = await open_repository(tmp_path / f"handoff-{workflow_type}.db")
    service = MediaJobApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        node_selector=node_id_for,
    )
    app = FastAPI()
    app.include_router(media_jobs_router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service

    body = {
        "workflow": workflow_type,
        "parameters": {
            "prompt": "safe mock handoff",
            "width": 512,
            "height": 512,
            "frame_count": 33,
            "seed": 42,
        },
    }
    if asset_id is not None:
        body["asset_id"] = asset_id
        asset_root = tmp_path / "assets"
        asset_root.mkdir()
        (asset_root / asset_id).write_bytes(b"mock-image")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/media/jobs",
            json=body,
            headers={"Idempotency-Key": f"handoff-{workflow_type}"},
        )
    assert response.status_code == 201
    job_id = response.json()["job_id"]
    created = await repository.get_job(job_id)
    assert created is not None
    assert created.node_id == node_id_for(workflow_type)

    prompt_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_calls
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "input.jpg", "subfolder": ""})
        if request.url.path == "/prompt":
            prompt_calls += 1
            return httpx.Response(200, json={"prompt_id": f"handoff-{workflow_type}"})
        if request.url.path == f"/history/handoff-{workflow_type}":
            return httpx.Response(
                200,
                json={
                    f"handoff-{workflow_type}": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "output": {
                                "videos": [
                                    {
                                        "filename": "result.mp4",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"mock-video")
        return httpx.Response(404)

    adapter = ComfyUIAdapter(
        nodes(),
        workflow_root=WORKFLOW_ROOT,
        transport=httpx.MockTransport(handler),
    )
    executor = RecoverableComfyUIExecutor(
        repository,
        adapter,
        managed_asset_root=tmp_path / "assets",
        managed_output_root=tmp_path / "outputs",
        history_poll_interval_seconds=0,
    )
    worker = MediaJobWorker(
        repository,
        executor,
        worker_id="handoff-worker",
        lease_seconds=2,
        heartbeat_seconds=0.1,
    )
    assert await worker.run_once() == 1
    stored = await repository.get_job(job_id)
    assert stored is not None
    assert stored.status == JobStatus.SUCCEEDED.value
    assert stored.node_id == node_id_for(workflow_type)
    assert stored.comfyui_prompt_id == f"handoff-{workflow_type}"
    assert prompt_calls == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_prebound_node_mismatch_fails_before_provider_and_clears_lease(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "node-mismatch.db")
    create = make_create("a800_wan22_t2v_33f").model_copy(
        update={"node_id": "different-a800"}
    )
    job = (await repository.create_job(create)).job
    provider_calls = 0

    async def forbidden_provider(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(500)

    worker = make_worker(repository, tmp_path, forbidden_provider)
    assert await worker.run_once() == 1
    stored = await repository.get_job(job.job_id)
    assert stored is not None
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.VALIDATION.value
    assert stored.error_message is not None
    assert stored.error_message
    assert "persisted node_id does not match selected ComfyUI node" in stored.error_message
    for sensitive in (
        "different-a800",
        "http://mock-comfyui",
        "Authorization",
        "api_key",
        "credential",
    ):
        assert sensitive not in stored.error_message
    assert stored.submit_started_at is None
    assert stored.comfyui_prompt_id is None
    assert stored.lease_owner is None
    assert stored.lease_expires_at is None
    assert stored.heartbeat_at is None
    assert provider_calls == 0
    await engine.dispose()


@pytest.mark.parametrize("workflow_type", WORKFLOW_TYPES)
@pytest.mark.asyncio
async def test_each_workflow_waits_and_resumes_without_second_submission(
    tmp_path: Path,
    workflow_type: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"wait-{workflow_type}.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)
    submit_count = 0
    completed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_count
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.jpg"})
        if request.url.path == "/prompt":
            submit_count += 1
            return httpx.Response(200, json={"prompt_id": f"wait-{workflow_type}"})
        if request.url.path == f"/history/wait-{workflow_type}":
            if not completed:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    f"wait-{workflow_type}": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "x": {
                                "videos": [
                                    {
                                        "filename": "result.mp4",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={
                    "queue_running": [],
                    "queue_pending": [[1, f"wait-{workflow_type}"]],
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"completed-video")
        return httpx.Response(404)

    worker = make_worker(repository, tmp_path, handler, history_poll_interval_seconds=0)
    await worker.run_once()
    waiting = await repository.get_job(job.job_id)
    assert waiting.status == JobStatus.RUNNING.value
    assert waiting.comfyui_prompt_id == f"wait-{workflow_type}"
    completed = True
    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUCCEEDED.value
    assert submit_count == 1
    await engine.dispose()


@pytest.mark.parametrize("workflow_type", WORKFLOW_TYPES)
@pytest.mark.asyncio
async def test_each_workflow_remote_failure_is_terminal(
    tmp_path: Path,
    workflow_type: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"failed-{workflow_type}.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.jpg"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "failed-prompt"})
        if request.url.path == "/history/failed-prompt":
            return httpx.Response(
                200,
                json={
                    "failed-prompt": {
                        "status": {
                            "status_str": "error",
                            "completed": False,
                            "messages": ["safe remote failure"],
                        },
                        "outputs": {},
                    }
                },
            )
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.REMOTE_FAILED.value
    await engine.dispose()


@pytest.mark.parametrize("workflow_type", WORKFLOW_TYPES)
@pytest.mark.asyncio
async def test_each_workflow_completed_without_output_fails(
    tmp_path: Path,
    workflow_type: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"empty-{workflow_type}.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.jpg"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "empty-prompt"})
        if request.url.path == "/history/empty-prompt":
            return httpx.Response(
                200,
                json={
                    "empty-prompt": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {},
                    }
                },
            )
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.OUTPUT_MISSING.value
    await engine.dispose()


@pytest.mark.parametrize("workflow_type", WORKFLOW_TYPES)
@pytest.mark.asyncio
async def test_each_workflow_known_prompt_restart_never_submits_again(
    tmp_path: Path,
    workflow_type: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"restart-{workflow_type}.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    job = await repository.update_prompt_and_remote_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        comfyui_prompt_id=f"known-{workflow_type}",
        remote_status=RemoteJobStatus.QUEUED,
        submit_started_at=utc_now(),
    )
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )
    prompt_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_calls
        if request.url.path == "/prompt":
            prompt_calls += 1
            return httpx.Response(500)
        if request.url.path == f"/history/known-{workflow_type}":
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={"queue_running": [[1, f"known-{workflow_type}"]]},
            )
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler, history_poll_interval_seconds=0).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.RUNNING.value
    assert stored.comfyui_prompt_id == f"known-{workflow_type}"
    assert prompt_calls == 0
    await engine.dispose()


@pytest.mark.parametrize("workflow_type", WORKFLOW_TYPES)
@pytest.mark.asyncio
async def test_each_workflow_rejects_unsafe_output_path(
    tmp_path: Path,
    workflow_type: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"unsafe-{workflow_type}.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.jpg"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "unsafe-prompt"})
        if request.url.path == "/history/unsafe-prompt":
            return httpx.Response(
                200,
                json={
                    "unsafe-prompt": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "x": {
                                "videos": [
                                    {
                                        "filename": "../escape.mp4",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.OUTPUT_MISSING.value
    assert stored.output_metadata == []
    await engine.dispose()


@pytest.mark.parametrize(
    ("subfolder", "storage_type", "should_download"),
    [
        ("nested/video", "output", True),
        (".", "output", False),
        ("../escape", "output", False),
        ("/absolute", "output", False),
        ("C:/drive", "output", False),
        ("C:relative", "output", False),
        ("D:folder/file", "output", False),
        ("z:lowercase-drive", "output", False),
        ("\\\\server\\share", "output", False),
        ("nested\\windows", "output", False),
        ("nested", "input", False),
        ("nested", "", False),
    ],
)
@pytest.mark.asyncio
async def test_remote_output_reference_is_validated_before_view_download(
    tmp_path: Path,
    subfolder: str,
    storage_type: str,
    should_download: bool,
) -> None:
    repository, engine = await open_repository(tmp_path / "remote-reference.db")
    job = await create_workflow_job(
        repository,
        tmp_path / "assets",
        "a800_wan22_t2v_33f",
    )
    view_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal view_calls
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "remote-reference"})
        if request.url.path == "/history/remote-reference":
            return httpx.Response(
                200,
                json={
                    "remote-reference": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "x": {
                                "videos": [
                                    {
                                        "filename": "result.mp4",
                                        "subfolder": subfolder,
                                        "type": storage_type,
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            view_calls += 1
            return httpx.Response(200, content=b"video")
        raise AssertionError(f"unexpected request: {request.url}")

    worker = make_worker(repository, tmp_path, handler, history_poll_interval_seconds=0)
    await worker.run_once()
    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert view_calls == int(should_download)
    if should_download:
        assert stored.status == JobStatus.SUCCEEDED.value
        assert stored.output_metadata[0]["relative_path"] == f"{job.job_id}/000-result.mp4"
    else:
        assert stored.status == JobStatus.FAILED.value
        assert stored.error_category == ErrorCategory.OUTPUT_MISSING.value
        assert stored.output_metadata == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_runs_during_slow_history_http_call(tmp_path: Path) -> None:
    workflow_type = "a800_wan22_t2v_33f"
    repository, engine = await open_repository(tmp_path / "slow-http.db")
    job = await create_workflow_job(repository, tmp_path / "assets", workflow_type)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "slow-http"})
        if request.url.path == "/history/slow-http":
            before = await repository.get_job(job.job_id)
            await asyncio.sleep(0.12)
            after = await repository.get_job(job.job_id)
            assert after.version > before.version
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={"queue_running": [[1, "slow-http"]], "queue_pending": []},
            )
        return httpx.Response(404)

    adapter = ComfyUIAdapter(
        nodes(),
        workflow_root=WORKFLOW_ROOT,
        transport=httpx.MockTransport(handler),
    )
    executor = RecoverableComfyUIExecutor(
        repository,
        adapter,
        managed_asset_root=tmp_path / "assets",
        managed_output_root=tmp_path / "outputs",
        history_poll_interval_seconds=0,
    )
    worker = MediaJobWorker(
        repository,
        executor,
        worker_id="slow-http-worker",
        lease_seconds=0.2,
        heartbeat_seconds=0.02,
    )

    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.RUNNING.value
    assert stored.lease_owner is None
    await engine.dispose()
