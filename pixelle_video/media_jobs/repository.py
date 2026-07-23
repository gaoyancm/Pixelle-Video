"""Asynchronous repository and optimistic-concurrency primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .contracts import MediaJobCreate, MediaOutputMetadata, compute_request_hash
from .models import MediaJob, utc_now
from .state_machine import (
    ErrorCategory,
    JobStatus,
    RemoteJobStatus,
    RemoteTerminationStatus,
    is_terminal,
    validate_transition,
)


class IdempotencyConflictError(RuntimeError):
    """The idempotency key already belongs to a different request hash."""


class CASConflictError(RuntimeError):
    """Expected status/version no longer matches the persistent row."""


@dataclass(frozen=True)
class CreateJobResult:
    job: MediaJob
    created: bool


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_ -]?key|access[_ -]?key|secret[_ -]?key|password|token)"
    r"\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]+\b")


def sanitize_error_message(message: str, *, sensitive_values: Iterable[str] = ()) -> str:
    """Create a bounded user-safe error summary without credential-shaped values."""

    safe = _BEARER.sub("Bearer [REDACTED]", message)
    safe = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", safe)
    safe = _SK_TOKEN.sub("[REDACTED]", safe)
    for value in sensitive_values:
        if value:
            safe = safe.replace(value, "[REDACTED]")
    return safe[:1000]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MediaJobRepository:
    """Persist media jobs using short transactions and CAS updates."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_job(self, create: MediaJobCreate) -> CreateJobResult:
        request_hash = compute_request_hash(create.immutable_request_payload())
        job = MediaJob(
            job_id=create.job_id,
            workflow_type=create.workflow_type,
            workflow_key=create.workflow_key,
            executor_kind=create.executor_kind,
            provider=create.provider,
            node_id=create.node_id,
            status=JobStatus.QUEUED.value,
            input_json=create.input_json,
            input_assets_json=[asset.model_dump() for asset in create.input_assets_json],
            submission_token=create.submission_token,
            idempotency_key=create.idempotency_key,
            request_hash=request_hash,
            deadline_at=_utc(create.deadline_at),
            output_metadata=[],
            retry_count=0,
            version=1,
            remote_status=RemoteJobStatus.UNKNOWN.value,
            remote_termination_status=RemoteTerminationStatus.UNKNOWN.value,
        )

        async with self._session_factory() as session:
            try:
                async with session.begin():
                    session.add(job)
                    await session.flush()
            except IntegrityError:
                await session.rollback()
                if create.idempotency_key is None:
                    raise
                existing = await self._get_by_idempotency_key(session, create.idempotency_key)
                if existing is None:
                    raise
                if existing.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key is already associated with a different request"
                    ) from None
                return CreateJobResult(job=existing, created=False)

        return CreateJobResult(job=job, created=True)

    async def get_job(self, job_id: str) -> MediaJob | None:
        async with self._session_factory() as session:
            return await session.get(MediaJob, job_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> MediaJob | None:
        async with self._session_factory() as session:
            return await self._get_by_idempotency_key(session, idempotency_key)

    async def _get_by_idempotency_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> MediaJob | None:
        result = await session.execute(
            select(MediaJob).where(MediaJob.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none()

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MediaJob]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        statement: Select[tuple[MediaJob]] = select(MediaJob)
        if status is not None:
            statement = statement.where(MediaJob.status == status.value)
        statement = statement.order_by(MediaJob.created_at.desc()).limit(limit).offset(offset)
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return list(result.scalars())

    async def transition_status(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        target_status: JobStatus,
        error_category: ErrorCategory | None = None,
        error_message: str | None = None,
    ) -> MediaJob:
        if expected_status is JobStatus.SUBMITTING and target_status is JobStatus.QUEUED:
            raise ValueError(
                "submitting jobs may only be requeued by requeue_unsubmitted_job"
            )
        validate_transition(expected_status, target_status)
        now = utc_now()
        values: dict = {"status": target_status.value}
        if target_status in {JobStatus.SUBMITTING, JobStatus.RUNNING}:
            values["started_at"] = func.coalesce(MediaJob.started_at, now)
        if is_terminal(target_status):
            values["finished_at"] = now
        if error_category is not None:
            values["error_category"] = error_category.value
        if error_message is not None:
            values["error_message"] = sanitize_error_message(error_message)
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            values=values,
        )

    async def requeue_unsubmitted_job(
        self,
        job_id: str,
        *,
        expected_version: int,
    ) -> MediaJob:
        """Atomically recover work that crashed before remote submission began."""

        return await self._cas_update(
            job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.submit_started_at.is_(None),
                MediaJob.comfyui_prompt_id.is_(None),
                or_(
                    MediaJob.error_category.is_(None),
                    MediaJob.error_category != ErrorCategory.SUBMISSION_UNKNOWN.value,
                ),
            ),
            values={
                "status": JobStatus.QUEUED.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
            },
        )

    async def mark_submission_unknown(
        self,
        job_id: str,
        *,
        expected_version: int,
        error_message: str | None = None,
    ) -> MediaJob:
        """Persist uncertainty after submission began but no prompt ID was saved."""

        values = {"error_category": ErrorCategory.SUBMISSION_UNKNOWN.value}
        if error_message is not None:
            values["error_message"] = sanitize_error_message(error_message)
        return await self._cas_update(
            job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.submit_started_at.is_not(None),
                MediaJob.comfyui_prompt_id.is_(None),
            ),
            values=values,
        )

    async def set_safe_error(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        category: ErrorCategory,
        message: str,
        sensitive_values: Iterable[str] = (),
    ) -> MediaJob:
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            values={
                "error_category": category.value,
                "error_message": sanitize_error_message(
                    message, sensitive_values=sensitive_values
                ),
            },
        )

    async def update_prompt_and_remote_status(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        comfyui_prompt_id: str | None,
        remote_status: RemoteJobStatus,
        remote_termination_status: RemoteTerminationStatus | None = None,
        submit_started_at: datetime | None = None,
    ) -> MediaJob:
        values: dict = {
            "comfyui_prompt_id": comfyui_prompt_id,
            "remote_status": remote_status.value,
            "remote_status_updated_at": utc_now(),
        }
        if remote_termination_status is not None:
            values["remote_termination_status"] = remote_termination_status.value
        if submit_started_at is not None:
            values["submit_started_at"] = _utc(submit_started_at)
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            values=values,
        )

    async def claim_lease(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> MediaJob:
        if not lease_owner.strip():
            raise ValueError("lease_owner must not be blank")
        now = utc_now()
        normalized_expiry = _utc(lease_expires_at)
        if normalized_expiry is None or normalized_expiry <= now:
            raise ValueError("lease_expires_at must be in the future")
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            additional_conditions=(
                or_(
                    and_(
                        MediaJob.lease_owner.is_(None),
                        MediaJob.lease_expires_at.is_(None),
                    ),
                    MediaJob.lease_expires_at <= now,
                ),
            ),
            values={
                "lease_owner": lease_owner,
                "lease_expires_at": normalized_expiry,
                "heartbeat_at": now,
            },
        )

    async def set_lease(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> MediaJob:
        """Backward-compatible name for the guarded atomic lease claim."""

        return await self.claim_lease(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
        )

    async def update_heartbeat(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> MediaJob:
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            additional_conditions=(MediaJob.lease_owner == lease_owner,),
            values={
                "heartbeat_at": utc_now(),
                "lease_expires_at": _utc(lease_expires_at),
            },
        )

    async def clear_lease(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        lease_owner: str,
    ) -> MediaJob:
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            additional_conditions=(MediaJob.lease_owner == lease_owner,),
            values={
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
            },
        )

    async def write_output_metadata(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        outputs: Sequence[MediaOutputMetadata],
    ) -> MediaJob:
        serialized = [output.model_dump() for output in outputs]
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            values={"output_metadata": serialized},
        )

    async def _cas_update(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        values: dict,
        additional_conditions: tuple = (),
    ) -> MediaJob:
        values = {
            **values,
            "updated_at": utc_now(),
            "version": MediaJob.version + 1,
        }
        statement = (
            update(MediaJob)
            .where(
                MediaJob.job_id == job_id,
                MediaJob.status == expected_status.value,
                MediaJob.version == expected_version,
                *additional_conditions,
            )
            .values(**values)
            .returning(MediaJob)
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(statement)
                job = result.scalar_one_or_none()
                if job is None:
                    raise CASConflictError(
                        "media job status or version changed before the update"
                    )
        return job
