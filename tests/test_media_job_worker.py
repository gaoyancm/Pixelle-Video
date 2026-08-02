from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.database import create_media_jobs_engine, sqlite_url_for_path
from pixelle_video.media_jobs.models import Base, MediaJob, utc_now
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus
from pixelle_video.media_jobs.worker import LeaseHandle, LeaseLostError, MediaJobWorker


def make_create() -> MediaJobCreate:
    return MediaJobCreate(
        workflow_type="a800_wan22_t2v_33f",
        workflow_key="selfhost/video_a800_wan22_t2v_4step_33f_api.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id="a800",
        input_json={"prompt": "worker test"},
        deadline_at=utc_now() + timedelta(minutes=5),
    )


async def open_repository(
    database_path: Path,
) -> tuple[MediaJobRepository, object]:
    engine = create_media_jobs_engine(sqlite_url_for_path(database_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return MediaJobRepository(factory), engine


class ReleasingProcessor:
    def __init__(self, repository: MediaJobRepository, delay: float = 0):
        self.repository = repository
        self.delay = delay
        self.calls: list[str] = []
        self.versions: list[tuple[int, int]] = []

    async def process(self, job: MediaJob, lease: LeaseHandle) -> None:
        self.calls.append(job.job_id)
        before = lease.version
        if self.delay:
            await asyncio.sleep(self.delay)
        self.versions.append((before, lease.version))
        await lease.mutate(
            lambda status, version: self.repository.release_owned(
                job.job_id,
                expected_status=status,
                expected_version=version,
                lease_owner=lease.worker_id,
            )
        )


@pytest.mark.asyncio
async def test_recovery_candidates_are_processed_before_queued_priority_work(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "worker-recovery-order.db")
    recovery = (await repository.create_job(make_create())).job
    queued = (await repository.create_job(make_create().model_copy(update={"priority": 2}))).job
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob).where(MediaJob.job_id == recovery.job_id).values(status="submitting")
        )
    processor = ReleasingProcessor(repository)
    worker = MediaJobWorker(
        repository, processor, candidate_limit=1, lease_seconds=2, heartbeat_seconds=0.2
    )
    assert await worker.run_once(include_recovery=True) == 1
    assert processor.calls == [recovery.job_id]
    assert queued.job_id not in processor.calls
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_compete_and_only_one_processes_job(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "worker-race.db")
    job = (await repository.create_job(make_create())).job
    claimed = asyncio.Event()
    release = asyncio.Event()

    class HoldingProcessor(ReleasingProcessor):
        async def process(self, job: MediaJob, lease: LeaseHandle) -> None:
            self.calls.append(job.job_id)
            claimed.set()
            await release.wait()
            await lease.mutate(
                lambda status, version: self.repository.release_owned(
                    job.job_id,
                    expected_status=status,
                    expected_version=version,
                    lease_owner=lease.worker_id,
                )
            )

    first_processor = HoldingProcessor(repository)
    second_processor = ReleasingProcessor(repository, delay=0.05)
    first = MediaJobWorker(
        repository,
        first_processor,
        worker_id="worker-1",
        lease_seconds=2,
        heartbeat_seconds=0.2,
    )
    second = MediaJobWorker(
        repository,
        second_processor,
        worker_id="worker-2",
        lease_seconds=2,
        heartbeat_seconds=0.2,
    )

    first_run = asyncio.create_task(first.run_once())
    await asyncio.wait_for(claimed.wait(), timeout=1)
    second_processed = await second.run_once()
    release.set()
    first_processed = await first_run

    assert first_processed == 1
    assert second_processed == 0
    assert first_processor.calls + second_processor.calls == [job.job_id]
    await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_continues_during_slow_processor_and_syncs_version(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "heartbeat.db")
    await repository.create_job(make_create())
    processor = ReleasingProcessor(repository, delay=0.12)
    worker = MediaJobWorker(
        repository,
        processor,
        worker_id="heartbeat-worker",
        lease_seconds=0.2,
        heartbeat_seconds=0.02,
    )

    assert await worker.run_once() == 1

    before, after = processor.versions[0]
    assert after > before
    stored = (await repository.list_jobs())[0]
    assert stored.lease_owner is None
    assert stored.version > after
    await engine.dispose()


@pytest.mark.asyncio
async def test_stop_prevents_new_claims(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "stopped.db")
    await repository.create_job(make_create())
    processor = ReleasingProcessor(repository)
    worker = MediaJobWorker(
        repository,
        processor,
        worker_id="stopped-worker",
        lease_seconds=2,
        heartbeat_seconds=0.2,
    )
    worker.request_stop()

    assert await worker.run_once() == 0
    assert processor.calls == []
    stored = (await repository.list_jobs())[0]
    assert stored.lease_owner is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_heartbeat_task_finishes_after_processing(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "heartbeat-cleanup.db")
    await repository.create_job(make_create())
    worker = MediaJobWorker(
        repository,
        ReleasingProcessor(repository),
        worker_id="cleanup-worker",
        lease_seconds=2,
        heartbeat_seconds=0.02,
    )

    await worker.run_once()

    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("media-job-heartbeat-")
    ]
    assert leaked == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_takeover_prevents_old_worker_from_writing(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "old-owner.db")
    job = (await repository.create_job(make_create())).job
    old_claim = await repository.claim_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        lease_owner="old-worker",
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )
    old_handle = LeaseHandle(
        repository,
        old_claim,
        worker_id="old-worker",
        lease_seconds=2,
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(MediaJob)
            .where(MediaJob.job_id == job.job_id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )
    new_claim = await repository.claim_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=old_claim.version,
        lease_owner="new-worker",
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )

    with pytest.raises(LeaseLostError):
        await old_handle.mutate(
            lambda status, version: repository.transition_owned(
                job.job_id,
                expected_status=status,
                expected_version=version,
                lease_owner="old-worker",
                target_status=JobStatus.CANCELLED,
            )
        )
    stored = await repository.get_job(job.job_id)
    assert stored.lease_owner == "new-worker"
    assert stored.version == new_claim.version
    assert stored.status == JobStatus.QUEUED.value
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_owner_cannot_revive_lease_with_heartbeat(tmp_path: Path) -> None:
    repository, engine = await open_repository(tmp_path / "expired-heartbeat.db")
    job = (await repository.create_job(make_create())).job
    expired_claim = await repository.claim_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        lease_owner="expired-worker",
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(MediaJob)
            .where(MediaJob.job_id == job.job_id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )
    handle = LeaseHandle(
        repository,
        expired_claim,
        worker_id="expired-worker",
        lease_seconds=60,
    )

    with pytest.raises(LeaseLostError):
        await handle.heartbeat()

    stored = await repository.get_job(job.job_id)
    assert handle.lost
    assert stored.lease_owner == "expired-worker"
    assert stored.lease_expires_at <= utc_now()
    await engine.dispose()


@pytest.mark.asyncio
async def test_graceful_stop_waits_for_current_step_and_leaks_no_heartbeat(
    tmp_path: Path,
) -> None:
    repository, engine = await open_repository(tmp_path / "graceful.db")
    await repository.create_job(make_create())
    started = asyncio.Event()
    finish = asyncio.Event()

    class BlockingProcessor(ReleasingProcessor):
        async def process(self, job: MediaJob, lease: LeaseHandle) -> None:
            started.set()
            await finish.wait()
            await super().process(job, lease)

    processor = BlockingProcessor(repository)
    worker = MediaJobWorker(
        repository,
        processor,
        worker_id="graceful-worker",
        poll_interval_seconds=0.01,
        lease_seconds=2,
        heartbeat_seconds=0.02,
    )
    running = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(started.wait(), timeout=1)
    worker.request_stop()
    assert not running.done()
    finish.set()
    await asyncio.wait_for(running, timeout=1)

    assert len(processor.calls) == 1
    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("media-job-heartbeat-")
    ]
    assert leaked == []
    await engine.dispose()
