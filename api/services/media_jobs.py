"""Application service for the trusted-network persistent media-job API."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta

from api.schemas.media_jobs import MediaJobRequest
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
from pixelle_video.media_jobs.state_machine import JobStatus, can_cancel
from pixelle_video.services.comfyui_workflows import get_workflow_spec


class JobNotFoundError(RuntimeError):
    pass


class JobNotCancelableError(RuntimeError):
    pass


class JobNotRetryableError(RuntimeError):
    pass


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
    ):
        self.repository = repository
        self.config = config
        self.assets = assets
        self.node_selector = node_selector

    async def create(self, request: MediaJobRequest, idempotency_key: str) -> tuple[MediaJob, bool]:
        spec = get_workflow_spec(request.workflow)
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
