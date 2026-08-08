"""Asynchronous repository and optimistic-concurrency primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Sequence

from loguru import logger
from sqlalchemy import Select, and_, case, func, or_, select, update
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

if TYPE_CHECKING:
    from pixelle_video.audit import AuditRepository


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

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        audit: "AuditRepository | None" = None,
    ):
        self._session_factory = session_factory
        self._audit = audit

    async def _record_audit(
        self,
        *,
        event_type: str,
        scope_type: str,
        scope_id: str,
        details: dict | None = None,
        cost_snapshot: dict | None = None,
    ) -> None:
        """Best-effort audit recording that never affects job operations."""
        if self._audit is None:
            return
        try:
            await self._audit.record(
                event_type=event_type,
                scope_type=scope_type,
                scope_id=scope_id,
                details=details,
                cost_snapshot=cost_snapshot,
            )
        except Exception:
            logger.warning("audit event recording failed for {} {}", scope_type, scope_id)

    @staticmethod
    def _new_job(create: MediaJobCreate, request_hash: str) -> MediaJob:
        return MediaJob(
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
            retry_of_job_id=create.retry_of_job_id,
            priority=create.priority,
            version=1,
            remote_status=RemoteJobStatus.UNKNOWN.value,
            remote_termination_status=RemoteTerminationStatus.UNKNOWN.value,
        )

    async def create_job(self, create: MediaJobCreate) -> CreateJobResult:
        request_hash = compute_request_hash(create.immutable_request_payload())
        job = self._new_job(create, request_hash)

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

        await self._record_audit(
            event_type="job_created",
            scope_type="job",
            scope_id=job.job_id,
            details={
                "workflow_type": job.workflow_type,
                "provider": job.provider,
                "node_id": job.node_id,
                "retry_of_job_id": job.retry_of_job_id,
                "status": job.status,
            },
        )
        return CreateJobResult(job=job, created=True)

    async def create_job_with_assets(self, create: MediaJobCreate) -> CreateJobResult:
        """Create the job and its validated input relations in one transaction."""

        from pixelle_video.media_assets.contracts import AssetDirection, AssetKind, AssetState
        from pixelle_video.media_assets.models import MediaAsset, MediaJobAsset

        request_hash = compute_request_hash(create.immutable_request_payload())
        job = self._new_job(create, request_hash)
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    input_relations = []
                    for position, reference in enumerate(create.input_assets_json):
                        result = await session.execute(
                            update(MediaAsset)
                            .where(
                                MediaAsset.id == reference.asset_id,
                                MediaAsset.kind == AssetKind.INPUT.value,
                                MediaAsset.state == AssetState.AVAILABLE.value,
                            )
                            .values(updated_at=MediaAsset.updated_at)
                            .returning(MediaAsset)
                        )
                        asset = result.scalar_one_or_none()
                        if asset is None:
                            raise ValueError("input asset is unavailable")
                        input_relations.append(
                            MediaJobAsset(
                                job_id=job.job_id,
                                asset_id=asset.id,
                                direction=AssetDirection.INPUT.value,
                                role=reference.role or "input",
                                position=position,
                            )
                        )
                    session.add(job)
                    await session.flush()
                    session.add_all(input_relations)
                    await session.flush()
                return CreateJobResult(job=job, created=True)
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

        await self._record_audit(
            event_type="job_created",
            scope_type="job",
            scope_id=job.job_id,
            details={
                "workflow_type": job.workflow_type,
                "provider": job.provider,
                "node_id": job.node_id,
                "retry_of_job_id": job.retry_of_job_id,
                "status": job.status,
            },
        )
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
        statement = (
            statement.order_by(MediaJob.created_at.desc(), MediaJob.job_id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return list(result.scalars())

    async def create_retry_job(
        self,
        source_job_id: str,
        *,
        idempotency_key: str,
        deadline_at: datetime,
    ) -> CreateJobResult:
        """Atomically validate a retry source and create its new queued child."""

        async with self._session_factory() as session:
            try:
                async with session.begin():
                    source = await session.get(MediaJob, source_job_id)
                    if source is None:
                        raise LookupError("media job not found")
                    error = (
                        ErrorCategory(source.error_category)
                        if source.error_category is not None
                        else None
                    )
                    from .state_machine import can_retry

                    if not can_retry(JobStatus(source.status), error):
                        raise ValueError("media job is not retryable")
                    create = MediaJobCreate(
                        workflow_type=source.workflow_type,
                        workflow_key=source.workflow_key,
                        executor_kind=source.executor_kind,
                        provider=source.provider,
                        node_id=source.node_id,
                        input_json=source.input_json,
                        input_assets_json=source.input_assets_json,
                        idempotency_key=idempotency_key,
                        deadline_at=deadline_at,
                        retry_of_job_id=source.job_id,
                        priority=source.priority,
                    )
                    request_hash = compute_request_hash(create.immutable_request_payload())
                    child = MediaJob(
                        job_id=create.job_id,
                        workflow_type=create.workflow_type,
                        workflow_key=create.workflow_key,
                        executor_kind=create.executor_kind,
                        provider=create.provider,
                        node_id=create.node_id,
                        status=JobStatus.QUEUED.value,
                        input_json=create.input_json,
                        input_assets_json=[
                            asset.model_dump() for asset in create.input_assets_json
                        ],
                        submission_token=create.submission_token,
                        idempotency_key=create.idempotency_key,
                        request_hash=request_hash,
                        deadline_at=create.deadline_at,
                        output_metadata=[],
                        retry_count=source.retry_count + 1,
                        retry_of_job_id=source.job_id,
                        priority=create.priority,
                        version=1,
                        remote_status=RemoteJobStatus.UNKNOWN.value,
                        remote_termination_status=RemoteTerminationStatus.UNKNOWN.value,
                    )
                    session.add(child)
                    await session.flush()
                    from pixelle_video.media_assets.contracts import (
                        AssetDirection,
                        AssetState,
                    )
                    from pixelle_video.media_assets.models import MediaAsset, MediaJobAsset

                    relations = await session.execute(
                        select(MediaJobAsset, MediaAsset)
                        .join(MediaAsset, MediaAsset.id == MediaJobAsset.asset_id)
                        .where(
                            MediaJobAsset.job_id == source.job_id,
                            MediaJobAsset.direction == AssetDirection.INPUT.value,
                        )
                    )
                    for relation, asset in relations.tuples():
                        result = await session.execute(
                            update(MediaAsset)
                            .where(
                                MediaAsset.id == asset.id,
                                MediaAsset.state == AssetState.AVAILABLE.value,
                            )
                            .values(updated_at=MediaAsset.updated_at)
                            .returning(MediaAsset.id)
                        )
                        if result.scalar_one_or_none() is None:
                            raise ValueError("input asset is unavailable")
                        session.add(
                            MediaJobAsset(
                                job_id=child.job_id,
                                asset_id=relation.asset_id,
                                direction=AssetDirection.INPUT.value,
                                role=relation.role,
                                position=relation.position,
                            )
                        )
                return CreateJobResult(job=child, created=True)
            except IntegrityError:
                await session.rollback()
                existing = await self._get_by_idempotency_key(session, idempotency_key)
                if existing is None:
                    raise
                if existing.retry_of_job_id != source_job_id:
                    raise IdempotencyConflictError(
                        "idempotency key belongs to a different retry request"
                    ) from None
                return CreateJobResult(job=existing, created=False)

        await self._record_audit(
            event_type="job_created",
            scope_type="job",
            scope_id=child.job_id,
            details={
                "workflow_type": child.workflow_type,
                "provider": child.provider,
                "node_id": child.node_id,
                "retry_of_job_id": child.retry_of_job_id,
                "status": child.status,
            },
        )
        return CreateJobResult(job=child, created=True)

    async def list_claim_candidates(
        self,
        *,
        now: datetime,
        limit: int = 100,
        statuses: Sequence[JobStatus] | None = None,
    ) -> list[MediaJob]:
        """List non-terminal work whose retry time and lease allow a claim attempt."""

        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = _utc(now)
        claim_statuses = statuses or (
            JobStatus.QUEUED,
            JobStatus.SUBMITTING,
            JobStatus.RUNNING,
        )
        # awaiting_human jobs are never claimable: a human decision is required.
        claim_statuses = tuple(
            status for status in claim_statuses if status is not JobStatus.AWAITING_HUMAN
        )
        if not claim_statuses:
            return []
        statement = select(MediaJob).where(
            MediaJob.status.in_(tuple(status.value for status in claim_statuses)),
            MediaJob.status != JobStatus.AWAITING_HUMAN.value,
            or_(
                MediaJob.next_attempt_at.is_(None),
                MediaJob.next_attempt_at <= now,
            ),
            or_(
                and_(
                    MediaJob.lease_owner.is_(None),
                    MediaJob.lease_expires_at.is_(None),
                ),
                MediaJob.lease_expires_at <= now,
            ),
        )
        if set(claim_statuses) == {JobStatus.QUEUED}:
            statement = statement.order_by(
                MediaJob.priority.desc(), MediaJob.created_at.asc(), MediaJob.job_id.asc()
            )
        else:
            queued_rank = case((MediaJob.status == JobStatus.QUEUED.value, 1), else_=0)
            queued_priority = case(
                (MediaJob.status == JobStatus.QUEUED.value, MediaJob.priority), else_=0
            )
            statement = statement.order_by(
                queued_rank.asc(),
                queued_priority.desc(),
                MediaJob.created_at.asc(),
                MediaJob.job_id.asc(),
            )
        statement = statement.limit(limit)
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
            raise ValueError("submitting jobs may only be requeued by requeue_unsubmitted_job")
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
        lease_owner: str | None = None,
    ) -> MediaJob:
        """Atomically recover work that crashed before remote submission began."""

        owner_condition = (MediaJob.lease_owner == lease_owner,) if lease_owner is not None else ()
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
                *owner_condition,
            ),
            values={
                "status": JobStatus.QUEUED.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
            },
        )

    async def transition_owned(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        lease_owner: str,
        target_status: JobStatus,
        error_category: ErrorCategory | None = None,
        error_message: str | None = None,
    ) -> MediaJob:
        """Transition state only while the caller still owns the durable lease."""

        validate_transition(expected_status, target_status)
        now = utc_now()
        values: dict = {"status": target_status.value}
        if target_status in {JobStatus.SUBMITTING, JobStatus.RUNNING}:
            values["started_at"] = func.coalesce(MediaJob.started_at, now)
        if is_terminal(target_status):
            values.update(
                {
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "next_attempt_at": None,
                }
            )
        if error_category is not None:
            values["error_category"] = error_category.value
        if error_message is not None:
            values["error_message"] = sanitize_error_message(error_message)
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            additional_conditions=(MediaJob.lease_owner == lease_owner,),
            values=values,
        )

    async def mark_submit_started_owned(
        self,
        job_id: str,
        *,
        expected_version: int,
        lease_owner: str,
        submit_started_at: datetime,
    ) -> MediaJob:
        """Persist the remote-side-effect boundary before POST /prompt."""

        return await self._cas_update(
            job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.lease_owner == lease_owner,
                MediaJob.submit_started_at.is_(None),
                MediaJob.comfyui_prompt_id.is_(None),
            ),
            values={"submit_started_at": _utc(submit_started_at)},
        )

    async def record_prompt_owned(
        self,
        job_id: str,
        *,
        expected_version: int,
        lease_owner: str,
        prompt_id: str,
        remote_status: RemoteJobStatus = RemoteJobStatus.QUEUED,
    ) -> MediaJob:
        """Persist a trusted prompt ID and enter recoverable tracking."""

        if not prompt_id.strip():
            raise ValueError("prompt_id must not be blank")
        return await self._cas_update(
            job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.lease_owner == lease_owner,
                MediaJob.submit_started_at.is_not(None),
                MediaJob.comfyui_prompt_id.is_(None),
            ),
            values={
                "status": JobStatus.RUNNING.value,
                "comfyui_prompt_id": prompt_id,
                "remote_status": remote_status.value,
                "remote_status_updated_at": utc_now(),
                "error_category": None,
                "error_message": None,
            },
        )

    async def mark_submission_unknown_owned(
        self,
        job_id: str,
        *,
        expected_version: int,
        lease_owner: str,
        error_message: str,
    ) -> MediaJob:
        """Persist submission uncertainty without making the job retryable."""

        return await self._cas_update(
            job_id,
            expected_status=JobStatus.SUBMITTING,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.lease_owner == lease_owner,
                MediaJob.submit_started_at.is_not(None),
                MediaJob.comfyui_prompt_id.is_(None),
            ),
            values={
                "error_category": ErrorCategory.SUBMISSION_UNKNOWN.value,
                "error_message": sanitize_error_message(error_message),
            },
        )

    async def update_remote_status_owned(
        self,
        job_id: str,
        *,
        expected_version: int,
        lease_owner: str,
        remote_status: RemoteJobStatus,
    ) -> MediaJob:
        return await self._cas_update(
            job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.lease_owner == lease_owner,
                MediaJob.comfyui_prompt_id.is_not(None),
            ),
            values={
                "remote_status": remote_status.value,
                "remote_status_updated_at": utc_now(),
            },
        )

    async def write_outputs_owned(
        self,
        job_id: str,
        *,
        expected_version: int,
        lease_owner: str,
        outputs: Sequence[MediaOutputMetadata],
    ) -> MediaJob:
        return await self._cas_update(
            job_id,
            expected_status=JobStatus.RUNNING,
            expected_version=expected_version,
            additional_conditions=(MediaJob.lease_owner == lease_owner,),
            values={"output_metadata": [output.model_dump() for output in outputs]},
        )

    async def release_owned(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        lease_owner: str,
        next_attempt_at: datetime | None = None,
    ) -> MediaJob:
        """Release a non-terminal lease after one bounded execution step."""

        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            additional_conditions=(MediaJob.lease_owner == lease_owner,),
            values={
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "next_attempt_at": _utc(next_attempt_at),
            },
        )

    async def request_cancellation(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        requested_at: datetime,
    ) -> MediaJob:
        """Persist a cancellation request without changing the task state."""

        if is_terminal(expected_status):
            raise ValueError("terminal jobs cannot receive cancellation requests")
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            values={"cancel_requested_at": _utc(requested_at)},
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
                "error_message": sanitize_error_message(message, sensitive_values=sensitive_values),
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
        now = utc_now()
        normalized_expiry = _utc(lease_expires_at)
        if normalized_expiry is None or normalized_expiry <= now:
            raise ValueError("heartbeat lease expiry must be in the future")
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            additional_conditions=(
                MediaJob.lease_owner == lease_owner,
                MediaJob.lease_expires_at > now,
            ),
            values={
                "heartbeat_at": now,
                "lease_expires_at": normalized_expiry,
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
                    raise CASConflictError("media job status or version changed before the update")
        target_status = values.get("status")
        if target_status is not None and target_status != expected_status.value:
            await self._record_audit(
                event_type="job_status_changed",
                scope_type="job",
                scope_id=job_id,
                details={
                    "from": expected_status.value,
                    "to": target_status,
                    "version": expected_version + 1,
                },
            )
        return job

    async def set_budget_fields(
        self,
        job_id: str,
        *,
        expected_status: JobStatus,
        expected_version: int,
        estimated_cost: float | None = None,
        actual_cost: float | None = None,
        budget_warning: str | None = None,
    ) -> MediaJob:
        """Persist phase 03-F cost fields without changing job status."""
        values: dict = {}
        if estimated_cost is not None:
            values["estimated_cost"] = estimated_cost
        if actual_cost is not None:
            values["actual_cost"] = actual_cost
        if budget_warning is not None:
            values["budget_warning"] = budget_warning
        if not values:
            raise ValueError("at least one budget field must be provided")
        return await self._cas_update(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            values=values,
        )
