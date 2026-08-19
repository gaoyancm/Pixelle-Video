"""Phase 10 task 2: executor dispatcher and llm_caption processor.

Verifies the worker routes each persisted ``executor_kind`` to its processor,
that unknown kinds reach a diagnosable terminal state without crashing, and
that ``llm_caption`` jobs generate and persist a real JSON caption output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pixelle_video.media_jobs.contracts import KNOWN_EXECUTOR_KINDS, MediaJobCreate
from pixelle_video.media_jobs.dispatcher import (
    DispatchingJobProcessor,
    LLMCaptionJobProcessor,
)
from pixelle_video.media_jobs.models import Base, MediaJob
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import ErrorCategory, JobStatus
from pixelle_video.media_jobs.worker import MediaJobWorker


async def _open_repository(tmp_path: Path) -> tuple[MediaJobRepository, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'dispatcher.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return MediaJobRepository(factory), engine


def _caption_create(prompt: str | None = "写出产品卖点文案") -> MediaJobCreate:
    return MediaJobCreate(
        workflow_type="llm_caption",
        workflow_key="caption.json",
        executor_kind="llm_caption",
        provider="llm_caption",
        node_id=None,
        input_json={"prompt_hint": prompt} if prompt is not None else {},
        input_assets_json=[],
        idempotency_key="caption-job",
    )


async def _fake_llm(prompt: str) -> str:
    return f"文案：{prompt}"


async def test_llm_caption_succeeds_and_persists_json_output(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)
    processor = LLMCaptionJobProcessor(
        repository,
        llm_caller=_fake_llm,
        managed_output_root=tmp_path / "outputs",
    )
    worker = MediaJobWorker(
        repository, processor, worker_id="caption-worker",
        lease_seconds=2, heartbeat_seconds=0.1,
    )
    job = (await repository.create_job(_caption_create())).job

    assert await worker.run_once() == 1

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUCCEEDED.value
    assert stored.output_metadata
    meta = stored.output_metadata[0]
    assert meta["media_type"] == "text"
    assert meta["mime_type"] == "application/json"
    output_path = tmp_path / "outputs" / Path(*meta["relative_path"].split("/"))
    assert output_path.is_file()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "产品卖点文案" in payload["text"]
    await engine.dispose()


async def test_llm_caption_fails_on_llm_error(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)

    async def broken_llm(_prompt: str) -> str:
        raise RuntimeError("provider down")

    processor = LLMCaptionJobProcessor(
        repository, llm_caller=broken_llm, managed_output_root=tmp_path / "outputs"
    )
    worker = MediaJobWorker(repository, processor, worker_id="w",
                            lease_seconds=2, heartbeat_seconds=0.1)
    job = (await repository.create_job(_caption_create())).job

    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.INTERNAL.value
    assert stored.output_metadata == []
    await engine.dispose()


async def test_llm_caption_rejects_missing_prompt(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)
    processor = LLMCaptionJobProcessor(
        repository, llm_caller=_fake_llm, managed_output_root=tmp_path / "outputs"
    )
    worker = MediaJobWorker(repository, processor, worker_id="w",
                            lease_seconds=2, heartbeat_seconds=0.1)
    job = (await repository.create_job(_caption_create(prompt=None))).job

    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.VALIDATION.value
    await engine.dispose()


async def test_llm_caption_rejects_empty_text(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)

    async def empty_llm(_prompt: str) -> str:
        return "   "

    processor = LLMCaptionJobProcessor(
        repository, llm_caller=empty_llm, managed_output_root=tmp_path / "outputs"
    )
    worker = MediaJobWorker(repository, processor, worker_id="w",
                            lease_seconds=2, heartbeat_seconds=0.1)
    job = (await repository.create_job(_caption_create())).job

    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.OUTPUT_MISSING.value
    await engine.dispose()


async def test_dispatcher_unknown_executor_is_terminal_and_does_not_crash(
    tmp_path: Path,
) -> None:
    repository, engine = await _open_repository(tmp_path)
    dispatcher = DispatchingJobProcessor(repository, {})
    worker = MediaJobWorker(repository, dispatcher, worker_id="w",
                            lease_seconds=2, heartbeat_seconds=0.1)
    # A historical legacy job predates the executor_kind allow-list, so it is
    # inserted through a valid kind and then mutated directly in the database.
    create = MediaJobCreate(
        workflow_type="image_qwen",
        workflow_key="selfhost/image_qwen.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id=None,
        input_json={"prompt": "test"},
        input_assets_json=[],
        idempotency_key="legacy-comfyui",
    )
    job = (await repository.create_job(create)).job
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.get(MediaJob, job.job_id)
        row.executor_kind = "comfyui"  # legacy kind, no longer registered
        await session.commit()

    assert await worker.run_once() == 1  # processed without raising

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.CONFIGURATION.value
    assert "no processor registered" in (stored.error_message or "")
    await engine.dispose()


def test_unknown_executor_kind_is_rejected_at_create() -> None:
    with pytest.raises(Exception):
        MediaJobCreate(
            workflow_type="x",
            workflow_key="x",
            executor_kind="bogus_kind",
            provider="bogus_kind",
            input_json={},
            input_assets_json=[],
        )


def test_known_executor_kinds_are_exactly_the_registered_set() -> None:
    assert KNOWN_EXECUTOR_KINDS == {"private_comfyui", "llm_caption"}


async def test_dispatcher_routes_caption_kind_to_caption_processor(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)
    caption = LLMCaptionJobProcessor(
        repository, llm_caller=_fake_llm, managed_output_root=tmp_path / "outputs"
    )
    dispatcher = DispatchingJobProcessor(repository, {"llm_caption": caption})
    worker = MediaJobWorker(repository, dispatcher, worker_id="w",
                            lease_seconds=2, heartbeat_seconds=0.1)
    job = (await repository.create_job(_caption_create())).job

    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUCCEEDED.value
    assert stored.output_metadata
    await engine.dispose()


async def test_cancel_requested_caption_is_cancelled(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)
    processor = LLMCaptionJobProcessor(
        repository, llm_caller=_fake_llm, managed_output_root=tmp_path / "outputs"
    )
    worker = MediaJobWorker(repository, processor, worker_id="w",
                            lease_seconds=2, heartbeat_seconds=0.1)
    create = _caption_create()
    job = (await repository.create_job(create)).job
    await repository.request_cancellation(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        requested_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    await worker.run_once()

    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.CANCELLED.value
    assert stored.error_category == ErrorCategory.CANCELLED.value
    await engine.dispose()


async def test_reclaimed_submitting_caption_resumes_once(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)
    job = (await repository.create_job(_caption_create())).job
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.get(MediaJob, job.job_id)
        row.status = JobStatus.SUBMITTING.value
        await session.commit()
    calls = 0

    async def counted(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return f"recovered:{prompt}"

    processor = LLMCaptionJobProcessor(
        repository, llm_caller=counted, managed_output_root=tmp_path / "outputs"
    )
    worker = MediaJobWorker(
        repository, processor, worker_id="recovery", lease_seconds=2, heartbeat_seconds=0.1
    )
    assert await worker.run_once() == 1
    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.SUCCEEDED.value
    assert calls == 1
    await engine.dispose()


async def test_reclaimed_running_caption_never_repeats_uncertain_call(tmp_path: Path) -> None:
    repository, engine = await _open_repository(tmp_path)
    job = (await repository.create_job(_caption_create())).job
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.get(MediaJob, job.job_id)
        row.status = JobStatus.RUNNING.value
        await session.commit()
    calls = 0

    async def must_not_call(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "duplicate"

    processor = LLMCaptionJobProcessor(
        repository, llm_caller=must_not_call, managed_output_root=tmp_path / "outputs"
    )
    worker = MediaJobWorker(
        repository, processor, worker_id="recovery", lease_seconds=2, heartbeat_seconds=0.1
    )
    assert await worker.run_once() == 1
    stored = await repository.get_job(job.job_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value
    assert calls == 0
    await engine.dispose()
