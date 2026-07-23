from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.database import create_media_jobs_engine, sqlite_url_for_path
from pixelle_video.media_jobs.executor import RecoverableComfyUIExecutor
from pixelle_video.media_jobs.models import Base, utc_now
from pixelle_video.media_jobs.repository import CASConflictError, MediaJobRepository
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


async def open_repository(
    database_path: Path,
) -> tuple[MediaJobRepository, object]:
    engine = create_media_jobs_engine(sqlite_url_for_path(database_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return MediaJobRepository(factory), engine


def make_create(*, deadline_at=None) -> MediaJobCreate:
    workflow_type = "a800_wan22_t2v_33f"
    return MediaJobCreate(
        workflow_type=workflow_type,
        workflow_key=WORKFLOW_SPECS[workflow_type].workflow_key,
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id="a800",
        input_json={"prompt": "recovery test"},
        deadline_at=deadline_at or utc_now() + timedelta(minutes=5),
    )


def make_worker(
    repository: MediaJobRepository,
    tmp_path: Path,
    handler,
    *,
    worker_id: str = "recovery-worker",
) -> MediaJobWorker:
    adapter = ComfyUIAdapter(
        [
            {
                "id": "a800",
                "name": "A800",
                "base_url": "http://mock-comfyui",
                "workflow_types": ["a800_wan22_t2v_33f"],
                "enabled": True,
                "timeout_seconds": 10,
                "concurrency": 1,
            }
        ],
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
    return MediaJobWorker(
        repository,
        executor,
        worker_id=worker_id,
        lease_seconds=2,
        heartbeat_seconds=0.1,
    )


async def make_submission_unknown(repository: MediaJobRepository):
    job = (await repository.create_job(make_create())).job
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
        comfyui_prompt_id=None,
        remote_status=RemoteJobStatus.UNKNOWN,
        submit_started_at=utc_now(),
    )
    return await repository.mark_submission_unknown(
        job.job_id,
        expected_version=job.version,
        error_message="submission response unknown",
    )


@pytest.mark.asyncio
async def test_crash_before_submit_marker_is_safely_requeued(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "safe-requeue.db")
    job = (await repository.create_job(make_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"ComfyUI must not be called: {request.url.path}")

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.QUEUED.value
    assert stored.submit_started_at is None
    assert stored.lease_owner is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_prompt_timeout_becomes_unknown_and_never_auto_resubmits(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "timeout-unknown.db")
    job = (await repository.create_job(make_create())).job
    prompt_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_calls
        if request.url.path == "/prompt":
            prompt_calls += 1
            raise httpx.ReadTimeout("response lost", request=request)
        if request.url.path in {"/queue", "/history"}:
            return httpx.Response(
                200,
                json={"queue_running": [], "queue_pending": []}
                if request.url.path == "/queue"
                else {},
            )
        return httpx.Response(404)

    worker = make_worker(repository, tmp_path, handler)
    await worker.run_once()
    unknown = await repository.get_job(job.job_id)
    assert unknown.status == JobStatus.SUBMITTING.value
    assert unknown.submit_started_at is not None
    assert unknown.comfyui_prompt_id is None
    assert unknown.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value

    await worker.run_once()
    repeated = await repository.get_job(job.job_id)
    assert repeated.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value
    assert repeated.comfyui_prompt_id is None
    assert prompt_calls == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_explicit_token_match_recovers_prompt_id(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "unique-match.db")
    job = await make_submission_unknown(repository)

    prompt_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_calls
        if request.url.path == "/prompt":
            prompt_calls += 1
            raise AssertionError("unknown submission must not be resubmitted")
        if request.url.path == "/history/recovered-prompt":
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={
                    "queue_running": [
                        [
                            1,
                            "recovered-prompt",
                            {},
                            {"pixelle_submission_token": job.submission_token},
                        ]
                    ],
                    "queue_pending": [],
                },
            )
        if request.url.path == "/history":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    worker = make_worker(repository, tmp_path, handler)
    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.RUNNING.value
    assert stored.comfyui_prompt_id == "recovered-prompt"
    assert stored.error_category is None
    await worker.run_once()
    repeated = await repository.get_job(job.job_id)
    assert repeated.comfyui_prompt_id == "recovered-prompt"
    assert prompt_calls == 0
    await engine.dispose()


@pytest.mark.parametrize("mode", ["zero", "multiple", "incomplete", "unreachable"])
@pytest.mark.asyncio
async def test_non_unique_or_unavailable_reconciliation_stays_unknown(
    tmp_path: Path,
    mode: str,
) -> None:
    repository, engine = await open_repository(tmp_path / f"{mode}.db")
    job = await make_submission_unknown(repository)

    async def handler(request: httpx.Request) -> httpx.Response:
        if mode == "unreachable":
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/queue":
            items = []
            if mode == "multiple":
                items = [
                    [1, "one", {}, {"pixelle_submission_token": job.submission_token}],
                    [2, "two", {}, {"pixelle_submission_token": job.submission_token}],
                ]
            if mode == "incomplete":
                items = [[1, "no-explicit-token", {}, {"client_id": job.submission_token}]]
            return httpx.Response(
                200,
                json={"queue_running": items, "queue_pending": []},
            )
        if request.url.path == "/history":
            return httpx.Response(200, json={})
        if request.url.path == "/prompt":
            raise AssertionError("unknown submission must not be resubmitted")
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUBMITTING.value
    assert stored.comfyui_prompt_id is None
    assert stored.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value
    await engine.dispose()


@pytest.mark.asyncio
async def test_lease_loss_during_reconciliation_prevents_prompt_write(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "lost-reconcile.db")
    job = await make_submission_unknown(repository)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/queue":
            stored = await repository.get_job(job.job_id)
            await repository.clear_lease(
                job.job_id,
                expected_status=JobStatus.SUBMITTING,
                expected_version=stored.version,
                lease_owner="recovery-worker",
            )
            return httpx.Response(
                200,
                json={
                    "queue_running": [
                        [
                            1,
                            "must-not-persist",
                            {},
                            {"pixelle_submission_token": job.submission_token},
                        ]
                    ],
                    "queue_pending": [],
                },
            )
        if request.url.path == "/history":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.comfyui_prompt_id is None
    assert stored.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value
    await engine.dispose()


@pytest.mark.asyncio
async def test_conflicting_queue_and_history_matches_stay_unknown(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "conflicting-match.db")
    job = await make_submission_unknown(repository)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={
                    "queue_running": [
                        [
                            1,
                            "queue-prompt",
                            {},
                            {"pixelle_submission_token": job.submission_token},
                        ]
                    ],
                    "queue_pending": [],
                },
            )
        if request.url.path == "/history":
            return httpx.Response(
                200,
                json={
                    "history-prompt": {
                        "prompt": [
                            2,
                            "history-prompt",
                            {},
                            {"pixelle_submission_token": job.submission_token},
                        ]
                    }
                },
            )
        return httpx.Response(404)

    await make_worker(repository, tmp_path, handler).run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUBMITTING.value
    assert stored.comfyui_prompt_id is None
    assert stored.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value
    await engine.dispose()


@pytest.mark.asyncio
async def test_running_without_prompt_id_is_failed_without_resubmission(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "invalid-running.db")
    job = (await repository.create_job(make_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"ComfyUI must not be called: {request.url.path}")

    await make_worker(repository, tmp_path, handler).run_once()
    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.INTERNAL.value
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_before_submission_never_calls_comfyui_and_is_terminal(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "cancel.db")
    job = (await repository.create_job(make_create())).job
    job = await repository.request_cancellation(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        requested_at=utc_now(),
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    worker = make_worker(repository, tmp_path, handler)
    await worker.run_once()
    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.CANCELLED.value
    assert stored.error_category == ErrorCategory.CANCELLED.value
    assert calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_deadline_times_out_without_interrupt_or_resurrection(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "deadline.db")
    job = (
        await repository.create_job(
            make_create(deadline_at=utc_now() - timedelta(seconds=1))
        )
    ).job
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    worker = make_worker(repository, tmp_path, handler)
    await worker.run_once()
    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.TIMED_OUT.value
    assert stored.error_category == ErrorCategory.TIMEOUT.value
    assert "/interrupt" not in calls
    assert calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_after_prompt_is_platform_terminal_without_remote_interrupt(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "cancel-known.db")
    job = (await repository.create_job(make_create())).job
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
        comfyui_prompt_id="remote-still-running",
        remote_status=RemoteJobStatus.RUNNING,
        submit_started_at=utc_now(),
    )
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )
    await repository.request_cancellation(
        job.job_id,
        expected_status=JobStatus.RUNNING,
        expected_version=job.version,
        requested_at=utc_now(),
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    worker = make_worker(repository, tmp_path, handler)
    await worker.run_once()
    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.CANCELLED.value
    assert stored.comfyui_prompt_id == "remote-still-running"
    assert calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_deadline_survives_database_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "deadline-reopen.db"
    repository, engine = await open_repository(database_path)
    job = (
        await repository.create_job(
            make_create(deadline_at=utc_now() - timedelta(seconds=1))
        )
    ).job
    await engine.dispose()
    reopened, reopened_engine = await open_repository(database_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"ComfyUI must not be called: {request.url.path}")

    await make_worker(reopened, tmp_path, handler).run_once()

    stored = await reopened.get_job(job.job_id)
    assert stored.deadline_at is not None
    assert stored.status == JobStatus.TIMED_OUT.value
    await reopened_engine.dispose()


@pytest.mark.asyncio
async def test_completed_unsafe_remote_path_is_not_persisted(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "unsafe-output.db")
    job = (await repository.create_job(make_create())).job

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "unsafe-output"})
        if request.url.path == "/history/unsafe-output":
            return httpx.Response(
                200,
                json={
                    "unsafe-output": {
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
    assert not (tmp_path / "escape.mp4").exists()
    await engine.dispose()


@pytest.mark.parametrize(
    "targets",
    [
        (JobStatus.SUCCEEDED, JobStatus.CANCELLED),
        (JobStatus.SUCCEEDED, JobStatus.TIMED_OUT),
        (JobStatus.CANCELLED, JobStatus.TIMED_OUT),
    ],
)
@pytest.mark.asyncio
async def test_each_terminal_pair_has_exactly_one_cas_winner(
    tmp_path: Path,
    targets: tuple[JobStatus, JobStatus],
) -> None:
    repository, engine = await open_repository(
        tmp_path / f"race-{targets[0].value}-{targets[1].value}.db"
    )
    job = (await repository.create_job(make_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )

    results = await asyncio.gather(
        *(
            repository.transition_status(
                job.job_id,
                expected_status=JobStatus.RUNNING,
                expected_version=job.version,
                target_status=target,
            )
            for target in targets
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CASConflictError) for result in results) == 1
    stored = await repository.get_job(job.job_id)
    assert stored.status in {target.value for target in targets}
    await engine.dispose()


@pytest.mark.parametrize(
    "target_order",
    [
        (JobStatus.SUCCEEDED, JobStatus.CANCELLED, JobStatus.TIMED_OUT),
        (JobStatus.CANCELLED, JobStatus.TIMED_OUT, JobStatus.SUCCEEDED),
        (JobStatus.TIMED_OUT, JobStatus.SUCCEEDED, JobStatus.CANCELLED),
    ],
)
@pytest.mark.asyncio
async def test_three_terminal_states_have_exactly_one_cas_winner(
    tmp_path: Path,
    target_order: tuple[JobStatus, JobStatus, JobStatus],
) -> None:
    repository, engine = await open_repository(
        tmp_path / f"three-way-{'-'.join(target.value for target in target_order)}.db"
    )
    job = (await repository.create_job(make_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )
    start = asyncio.Event()

    async def race_terminal(target: JobStatus, delay: float):
        await start.wait()
        await asyncio.sleep(delay)
        return await repository.transition_status(
            job.job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=job.version,
            target_status=target,
        )

    tasks = [
        asyncio.create_task(race_terminal(target, index * 0.001))
        for index, target in enumerate(target_order)
    ]
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CASConflictError) for result in results) == 2
    stored = await repository.get_job(job.job_id)
    assert stored.status in {target.value for target in target_order}
    with pytest.raises(CASConflictError):
        await repository.transition_status(
            job.job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=job.version,
            target_status=JobStatus.SUCCEEDED,
        )
    await engine.dispose()
