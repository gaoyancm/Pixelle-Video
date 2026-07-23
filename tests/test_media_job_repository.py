from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.contracts import (
    MediaInputAsset,
    MediaJobCreate,
    MediaOutputMetadata,
    compute_request_hash,
)
from pixelle_video.media_jobs.database import create_media_jobs_engine, sqlite_url_for_path
from pixelle_video.media_jobs.models import Base, MediaJob, utc_now
from pixelle_video.media_jobs.repository import (
    CASConflictError,
    IdempotencyConflictError,
    MediaJobRepository,
)
from pixelle_video.media_jobs.state_machine import (
    ErrorCategory,
    JobStatus,
    RemoteJobStatus,
    RemoteTerminationStatus,
    can_retry,
)


def make_create(
    *,
    idempotency_key: str | None = None,
    prompt: str = "a calm lake",
) -> MediaJobCreate:
    input_json = {"prompt": prompt, "width": 640, "height": 640}
    return MediaJobCreate(
        workflow_type="a800_wan22_t2v_33f",
        workflow_key="selfhost/video_a800_wan22_t2v_4step_33f_api.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id="a800",
        input_json=input_json,
        input_assets_json=[],
        idempotency_key=idempotency_key,
        deadline_at=utc_now() + timedelta(minutes=15),
    )


async def open_repository(
    database_path: Path,
) -> tuple[MediaJobRepository, object, async_sessionmaker[AsyncSession]]:
    engine = create_media_jobs_engine(sqlite_url_for_path(database_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    return MediaJobRepository(session_factory), engine, session_factory


@pytest.mark.asyncio
async def test_create_read_json_round_trip_and_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "reopen.db"
    repository, engine, _ = await open_repository(database_path)
    create = make_create()
    create.input_assets_json = [MediaInputAsset(asset_id="asset-1", role="first_frame")]

    result = await repository.create_job(create)
    loaded = await repository.get_job(result.job.job_id)

    assert result.created is True
    assert loaded is not None
    assert loaded.input_json == create.input_json
    assert loaded.input_assets_json == [{"asset_id": "asset-1", "role": "first_frame"}]
    assert loaded.status == JobStatus.QUEUED.value
    assert loaded.version == 1
    assert loaded.created_at.tzinfo is not None
    await engine.dispose()

    reopened_repository, reopened_engine, _ = await open_repository(database_path)
    reopened = await reopened_repository.get_job(result.job.job_id)
    assert reopened is not None
    assert reopened.request_hash == compute_request_hash(create.immutable_request_payload())
    assert reopened.input_json == create.input_json
    await reopened_engine.dispose()


@pytest.mark.asyncio
async def test_same_idempotency_key_and_hash_returns_existing_job(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "same-idempotency.db")

    first = await repository.create_job(make_create(idempotency_key="same-key"))
    second = await repository.create_job(make_create(idempotency_key="same-key"))

    assert first.created is True
    assert second.created is False
    assert second.job.job_id == first.job.job_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_idempotency_key_and_different_hash_conflicts(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "conflicting-idempotency.db")
    await repository.create_job(make_create(idempotency_key="same-key", prompt="first"))

    with pytest.raises(IdempotencyConflictError):
        await repository.create_job(make_create(idempotency_key="same-key", prompt="second"))
    await engine.dispose()


def test_request_hash_canonicalizes_nested_objects_but_preserves_array_order() -> None:
    first = make_create()
    first.input_json = {"nested": {"b": 2, "a": 1}, "items": [1, 2]}
    second = make_create()
    second.input_json = {"items": [1, 2], "nested": {"a": 1, "b": 2}}
    reversed_items = make_create()
    reversed_items.input_json = {"nested": {"a": 1, "b": 2}, "items": [2, 1]}

    first_hash = compute_request_hash(first.immutable_request_payload())
    assert first_hash == compute_request_hash(second.immutable_request_payload())
    assert first_hash != compute_request_hash(reversed_items.immutable_request_payload())


def test_request_hash_covers_all_immutable_execution_fields_only() -> None:
    create = make_create(idempotency_key="key-one")
    payload = create.immutable_request_payload()
    baseline = compute_request_hash(payload)

    for field in (
        "workflow_type",
        "workflow_key",
        "executor_kind",
        "provider",
        "node_id",
        "input_json",
        "input_assets_json",
    ):
        changed = dict(payload)
        changed[field] = {"changed": True} if field == "input_json" else f"changed-{field}"
        if field == "input_assets_json":
            changed[field] = [{"asset_id": "changed", "role": None}]
        assert compute_request_hash(changed) != baseline

    extended = {
        **payload,
        "job_id": "ignored",
        "idempotency_key": "ignored",
        "submission_token": "ignored",
        "retry_count": 99,
        "status": "running",
    }
    assert compute_request_hash(extended) == baseline


def test_caller_cannot_supply_a_forged_request_hash() -> None:
    payload = make_create().model_dump()
    payload["request_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="request_hash"):
        MediaJobCreate(**payload)


@pytest.mark.asyncio
async def test_nested_key_order_is_idempotently_equivalent(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "nested-order.db")
    first = make_create(idempotency_key="nested-key")
    first.input_json = {"nested": {"b": 2, "a": 1}, "items": [1, 2]}
    second = make_create(idempotency_key="nested-key")
    second.input_json = {"items": [1, 2], "nested": {"a": 1, "b": 2}}

    created = await repository.create_job(first)
    existing = await repository.create_job(second)

    assert created.created is True
    assert existing.created is False
    assert existing.job.job_id == created.job.job_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_idempotent_creation_creates_one_job(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "concurrent-idempotency.db")

    results = await asyncio.gather(
        *(repository.create_job(make_create(idempotency_key="concurrent-key")) for _ in range(8))
    )
    jobs = await repository.list_jobs()

    assert sum(result.created for result in results) == 1
    assert len({result.job.job_id for result in results}) == 1
    assert len(jobs) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_idempotency_key_always_creates_a_new_job(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "no-idempotency.db")

    first = await repository.create_job(make_create())
    second = await repository.create_job(make_create())

    assert first.created and second.created
    assert first.job.job_id != second.job.job_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_cas_transition_succeeds_and_stale_version_or_status_fails(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "cas.db")
    created = (await repository.create_job(make_create())).job

    submitting = await repository.transition_status(
        created.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=1,
        target_status=JobStatus.SUBMITTING,
    )
    assert submitting.status == JobStatus.SUBMITTING.value
    assert submitting.version == 2

    with pytest.raises(CASConflictError):
        await repository.transition_status(
            created.job_id,
            expected_status=JobStatus.QUEUED,
            expected_version=1,
            target_status=JobStatus.CANCELLED,
        )
    with pytest.raises(CASConflictError):
        await repository.transition_status(
            created.job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=1,
            target_status=JobStatus.RUNNING,
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_complete_and_timeout_race_has_one_winner(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "cas-race.db")
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
        repository.transition_status(
            job.job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=job.version,
            target_status=JobStatus.CANCELLED,
        ),
        repository.transition_status(
            job.job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=job.version,
            target_status=JobStatus.SUCCEEDED,
        ),
        repository.transition_status(
            job.job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=job.version,
            target_status=JobStatus.TIMED_OUT,
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CASConflictError) for result in results) == 2
    persisted = await repository.get_job(job.job_id)
    assert persisted.status in {
        JobStatus.CANCELLED.value,
        JobStatus.SUCCEEDED.value,
        JobStatus.TIMED_OUT.value,
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_only_dedicated_guard_can_requeue_before_submit_starts(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "safe-requeue.db")
    job = (await repository.create_job(make_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )

    with pytest.raises(ValueError, match="requeue_unsubmitted_job"):
        await repository.transition_status(
            job.job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=job.version,
            target_status=JobStatus.QUEUED,
        )

    requeued = await repository.requeue_unsubmitted_job(
        job.job_id, expected_version=job.version
    )
    assert requeued.status == JobStatus.QUEUED.value
    assert requeued.version == job.version + 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_submission_unknown_cannot_be_requeued(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "unknown-requeue.db")
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
    with pytest.raises(CASConflictError):
        await repository.requeue_unsubmitted_job(
            job.job_id, expected_version=job.version
        )
    job = await repository.mark_submission_unknown(
        job.job_id,
        expected_version=job.version,
        error_message="remote submission response was not observed",
    )

    assert job.error_category == ErrorCategory.SUBMISSION_UNKNOWN.value
    with pytest.raises(CASConflictError):
        await repository.requeue_unsubmitted_job(
            job.job_id, expected_version=job.version
        )
    assert not can_retry(JobStatus.SUBMITTING, ErrorCategory.SUBMISSION_UNKNOWN)
    await engine.dispose()


@pytest.mark.asyncio
async def test_submitting_job_with_prompt_id_cannot_be_requeued(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "prompt-requeue.db")
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
        comfyui_prompt_id="prompt-known",
        remote_status=RemoteJobStatus.QUEUED,
        submit_started_at=utc_now(),
    )

    with pytest.raises(CASConflictError):
        await repository.requeue_unsubmitted_job(
            job.job_id, expected_version=job.version
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_lease_heartbeat_and_clear_are_persistent(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "lease.db")
    job = (await repository.create_job(make_create())).job
    first_expiry = utc_now() + timedelta(seconds=60)

    leased = await repository.set_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        lease_owner="worker-1",
        lease_expires_at=first_expiry,
    )
    assert leased.lease_owner == "worker-1"
    assert leased.heartbeat_at is not None

    extended = await repository.update_heartbeat(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=leased.version,
        lease_owner="worker-1",
        lease_expires_at=first_expiry + timedelta(seconds=60),
    )
    assert extended.version == leased.version + 1
    assert extended.lease_expires_at > leased.lease_expires_at

    cleared = await repository.clear_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=extended.version,
        lease_owner="worker-1",
    )
    assert cleared.lease_owner is None
    assert cleared.lease_expires_at is None
    assert cleared.heartbeat_at is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_valid_lease_cannot_be_stolen_and_non_owner_cannot_mutate_it(
    tmp_path: Path,
) -> None:
    repository, engine, _ = await open_repository(tmp_path / "lease-owner.db")
    job = (await repository.create_job(make_create())).job
    leased = await repository.claim_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        lease_owner="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=2),
    )

    with pytest.raises(CASConflictError):
        await repository.claim_lease(
            job.job_id,
            expected_status=JobStatus.QUEUED,
            expected_version=leased.version,
            lease_owner="worker-2",
            lease_expires_at=utc_now() + timedelta(minutes=3),
        )
    with pytest.raises(CASConflictError):
        await repository.update_heartbeat(
            job.job_id,
            expected_status=JobStatus.QUEUED,
            expected_version=leased.version,
            lease_owner="worker-2",
            lease_expires_at=utc_now() + timedelta(minutes=3),
        )
    with pytest.raises(CASConflictError):
        await repository.clear_lease(
            job.job_id,
            expected_status=JobStatus.QUEUED,
            expected_version=leased.version,
            lease_owner="worker-2",
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_can_be_claimed_but_stale_version_cannot(
    tmp_path: Path,
) -> None:
    repository, engine, session_factory = await open_repository(tmp_path / "expired.db")
    job = (await repository.create_job(make_create())).job
    leased = await repository.claim_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        lease_owner="worker-old",
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == job.job_id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )

    with pytest.raises(CASConflictError):
        await repository.claim_lease(
            job.job_id,
            expected_status=JobStatus.QUEUED,
            expected_version=job.version,
            lease_owner="worker-stale",
            lease_expires_at=utc_now() + timedelta(minutes=2),
        )
    reclaimed = await repository.claim_lease(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=leased.version,
        lease_owner="worker-new",
        lease_expires_at=utc_now() + timedelta(minutes=2),
    )
    assert reclaimed.lease_owner == "worker-new"
    assert reclaimed.version == leased.version + 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_lease_claim_has_one_winner(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "lease-race.db")
    job = (await repository.create_job(make_create())).job

    results = await asyncio.gather(
        *(
            repository.claim_lease(
                job.job_id,
                expected_status=JobStatus.QUEUED,
                expected_version=job.version,
                lease_owner=f"worker-{index}",
                lease_expires_at=utc_now() + timedelta(minutes=2),
            )
            for index in range(2)
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CASConflictError) for result in results) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_inconsistent_lease_is_not_silently_claimable(tmp_path: Path) -> None:
    repository, engine, session_factory = await open_repository(tmp_path / "inconsistent.db")
    job = (await repository.create_job(make_create())).job
    async with session_factory() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == job.job_id)
            .values(lease_owner="orphan-owner", lease_expires_at=None)
        )

    with pytest.raises(CASConflictError):
        await repository.claim_lease(
            job.job_id,
            expected_status=JobStatus.QUEUED,
            expected_version=job.version,
            lease_owner="worker-new",
            lease_expires_at=utc_now() + timedelta(minutes=2),
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_prompt_remote_status_and_termination_status_are_persistent(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "remote-status.db")
    job = (await repository.create_job(make_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    submit_started_at = utc_now()

    updated = await repository.update_prompt_and_remote_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        comfyui_prompt_id="prompt-123",
        remote_status=RemoteJobStatus.QUEUED,
        remote_termination_status=RemoteTerminationStatus.UNKNOWN,
        submit_started_at=submit_started_at,
    )

    assert updated.comfyui_prompt_id == "prompt-123"
    assert updated.remote_status == RemoteJobStatus.QUEUED.value
    assert updated.remote_termination_status == RemoteTerminationStatus.UNKNOWN.value
    assert updated.remote_status_updated_at is not None
    assert updated.submit_started_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_output_metadata_round_trip_and_absolute_paths_are_rejected(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "outputs.db")
    job = (await repository.create_job(make_create())).job
    output = MediaOutputMetadata(
        output_id="output-1",
        media_type="video",
        relative_path="media_jobs/job-1/result.mp4",
        size=1234,
        mime_type="video/mp4",
        sha256="a" * 64,
        width=640,
        height=640,
        duration=2.0,
        frame_count=33,
    )

    updated = await repository.write_output_metadata(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        outputs=[output],
    )
    assert updated.output_metadata == [output.model_dump()]

    with pytest.raises(ValidationError, match="absolute"):
        MediaOutputMetadata(
            output_id="bad",
            media_type="video",
            relative_path="C:/secret/result.mp4",
            size=1,
            mime_type="video/mp4",
            sha256="b" * 64,
        )
    with pytest.raises(ValidationError, match="traversal"):
        MediaOutputMetadata(
            output_id="bad-traversal",
            media_type="video",
            relative_path="media_jobs/../secret.mp4",
            size=1,
            mime_type="video/mp4",
            sha256="c" * 64,
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_safe_error_redacts_credentials(tmp_path: Path) -> None:
    repository, engine, _ = await open_repository(tmp_path / "safe-error.db")
    job = (await repository.create_job(make_create())).job
    test_key = "test-api-key-do-not-use"

    updated = await repository.set_safe_error(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        category=ErrorCategory.INTERNAL,
        message=f"provider failed api_key={test_key}; Authorization: Bearer secret-token",
        sensitive_values=[test_key],
    )

    assert test_key not in updated.error_message
    assert "secret-token" not in updated.error_message
    assert "[REDACTED]" in updated.error_message
    await engine.dispose()


def test_persisted_input_rejects_secret_fields() -> None:
    create = make_create()
    payload = create.model_dump()
    payload["input_json"] = {"prompt": "safe", "api_key": "test-only-secret"}

    with pytest.raises(ValidationError, match="secret field"):
        MediaJobCreate(**payload)


@pytest.mark.asyncio
async def test_sqlite_pragmas_are_enabled(tmp_path: Path) -> None:
    _, engine, session_factory = await open_repository(tmp_path / "pragmas.db")
    async with session_factory() as session:
        foreign_keys = (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
        busy_timeout = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
        journal_mode = (await session.execute(text("PRAGMA journal_mode"))).scalar_one()

    assert foreign_keys == 1
    assert busy_timeout == 5000
    assert journal_mode.lower() == "wal"
    await engine.dispose()
