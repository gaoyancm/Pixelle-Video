"""Application service for the trusted-network persistent media-job API."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta

from api.schemas.media_jobs import MediaJobRequest
from pixelle_video.audit import AuditRepository
from pixelle_video.budget import BudgetService
from pixelle_video.config.schema import MediaJobsConfig
from pixelle_video.media_assets import AssetService, AssetUnavailableError
from pixelle_video.media_jobs import (
    CASConflictError,
    MediaInputAsset,
    MediaJobCreate,
    MediaJobRepository,
    MediaJobsDisabledError,
)
from pixelle_video.media_jobs.models import MediaJob, utc_now
from pixelle_video.media_jobs.state_machine import ErrorCategory, JobStatus, can_cancel
from pixelle_video.services.comfyui_workflows import get_workflow_spec


class JobNotFoundError(RuntimeError):
    pass


class JobNotCancelableError(RuntimeError):
    pass


class JobNotRetryableError(RuntimeError):
    pass


class JobNotApprovalableError(RuntimeError):
    """The media job is not in a state that accepts human approval actions."""


def _scoped_key(operation: str, raw_key: str, source_job_id: str = "") -> str:
    material = f"{operation}\0{source_job_id}\0{raw_key}".encode("utf-8")
    return f"{operation}:{hashlib.sha256(material).hexdigest()}"


class MediaJobApplicationService:
    """Own API business rules and short transactional repository calls."""

    def __init__(
        self,
        repository: MediaJobRepository,
        config: MediaJobsConfig,
        assets: AssetService | None = None,
        *,
        node_selector: Callable[[str], str],
        budget: BudgetService | None = None,
        audit: AuditRepository | None = None,
    ):
        self.repository = repository
        self.config = config
        self.assets = assets
        self.node_selector = node_selector
        self.budget = budget
        self.audit = audit

    async def create(self, request: MediaJobRequest, idempotency_key: str) -> tuple[MediaJob, bool]:
        spec = get_workflow_spec(request.workflow)
        budget_decision = None
        if self.budget is not None:
            budget_decision = await self.budget.check_job_creation(request.workflow)
        try:
            node_id = self.node_selector(spec.workflow_type)
        except RuntimeError:
            raise MediaJobsDisabledError from None
        if request.asset_id is not None and self.assets is not None:
            asset = await self.assets.get_available_input(request.asset_id)
            if asset.media_type != "image":
                raise AssetUnavailableError
        create = MediaJobCreate(
            workflow_type=spec.workflow_type,
            workflow_key=spec.workflow_key,
            executor_kind="private_comfyui",
            provider="private_comfyui",
            node_id=node_id,
            input_json=request.public_parameters(),
            input_assets_json=(
                [MediaInputAsset(asset_id=request.asset_id, role="input_image")]
                if request.asset_id is not None
                else []
            ),
            idempotency_key=_scoped_key("create", idempotency_key),
            deadline_at=utc_now() + timedelta(seconds=self.config.default_timeout_seconds),
        )
        result = (
            await self.repository.create_job_with_assets(create)
            if self.assets is not None
            else await self.repository.create_job(create)
        )
        if self.budget is not None and result.created and budget_decision is not None:
            await self.budget.record_job_cost(
                result.job.job_id,
                status=JobStatus(result.job.status),
                expected_version=result.job.version,
                estimated_cost=budget_decision.estimated_cost,
                budget_warning=budget_decision.warning,
            )
        return result.job, result.created

    async def get(self, job_id: str) -> MediaJob:
        job = await self.repository.get_job(job_id)
        if job is None:
            raise JobNotFoundError
        return job

    async def list(
        self, *, status: JobStatus | None, limit: int, offset: int
    ) -> tuple[list[MediaJob], bool]:
        rows = await self.repository.list_jobs(
            status=status, limit=limit + 1, offset=offset
        )
        return rows[:limit], len(rows) > limit

    async def cancel(self, job_id: str) -> MediaJob:
        for _ in range(3):
            job = await self.get(job_id)
            if job.cancel_requested_at is not None:
                return job
            if not can_cancel(JobStatus(job.status)):
                raise JobNotCancelableError
            try:
                return await self.repository.request_cancellation(
                    job_id,
                    expected_status=JobStatus(job.status),
                    expected_version=job.version,
                    requested_at=utc_now(),
                )
            except CASConflictError:
                continue
        raise JobNotCancelableError

    async def retry(self, job_id: str, idempotency_key: str) -> tuple[MediaJob, bool]:
        try:
            result = await self.repository.create_retry_job(
                job_id,
                idempotency_key=_scoped_key("retry", idempotency_key, job_id),
                deadline_at=utc_now()
                + timedelta(seconds=self.config.default_timeout_seconds),
            )
        except LookupError:
            raise JobNotFoundError from None
        except ValueError:
            raise JobNotRetryableError from None
        return result.job, result.created

    async def request_approval(self, job_id: str, reason: str | None = None) -> MediaJob:
        """Suspend a running job into awaiting_human for a human decision."""
        for _ in range(3):
            job = await self.get(job_id)
            if job.status != JobStatus.RUNNING.value:
                raise JobNotApprovalableError
            try:
                updated = await self.repository.transition_status(
                    job_id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=job.version,
                    target_status=JobStatus.AWAITING_HUMAN,
                )
            except CASConflictError:
                continue
            await self._record_approval_audit(
                event_type="approval_requested", job_id=job_id, reason=reason
            )
            return updated
        raise JobNotApprovalableError

    async def approve(self, job_id: str) -> MediaJob:
        """Resume an awaiting_human job back to running."""
        for _ in range(3):
            job = await self.get(job_id)
            if job.status != JobStatus.AWAITING_HUMAN.value:
                raise JobNotApprovalableError
            try:
                updated = await self.repository.transition_status(
                    job_id,
                    expected_status=JobStatus.AWAITING_HUMAN,
                    expected_version=job.version,
                    target_status=JobStatus.RUNNING,
                )
            except CASConflictError:
                continue
            await self._record_approval_audit(event_type="approval_granted", job_id=job_id)
            return updated
        raise JobNotApprovalableError

    async def reject(self, job_id: str, reason: str | None = None) -> MediaJob:
        """Reject an awaiting_human job; the reason is recorded in the audit trail."""
        for _ in range(3):
            job = await self.get(job_id)
            if job.status != JobStatus.AWAITING_HUMAN.value:
                raise JobNotApprovalableError
            try:
                updated = await self.repository.transition_status(
                    job_id,
                    expected_status=JobStatus.AWAITING_HUMAN,
                    expected_version=job.version,
                    target_status=JobStatus.CANCELLED,
                    error_category=ErrorCategory.CANCELLED,
                    error_message=reason or "rejected by human operator",
                )
            except CASConflictError:
                continue
            await self._record_approval_audit(
                event_type="approval_rejected", job_id=job_id, reason=reason
            )
            return updated
        raise JobNotApprovalableError

    async def _record_approval_audit(
        self, *, event_type: str, job_id: str, reason: str | None = None
    ) -> None:
        if self.audit is None:
            return
        try:
            await self.audit.record(
                event_type=event_type,
                scope_type="job",
                scope_id=job_id,
                details={"reason": reason} if reason is not None else None,
            )
        except Exception:
            return
