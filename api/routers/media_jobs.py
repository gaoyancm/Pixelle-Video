"""Trusted-network control-plane endpoints for persistent media jobs."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import MediaJobServiceDep
from api.schemas.media_jobs import (
    ErrorResponse,
    MediaJobError,
    MediaJobListResponse,
    MediaJobOutput,
    MediaJobRequest,
    MediaJobResponse,
    validate_idempotency_key,
)
from api.services.media_jobs import (
    JobNotCancelableError,
    JobNotFoundError,
    JobNotRetryableError,
)
from pixelle_video.media_assets import AssetNotFoundError, AssetUnavailableError
from pixelle_video.media_jobs import IdempotencyConflictError, MediaJobsDisabledError
from pixelle_video.media_jobs.models import MediaJob
from pixelle_video.media_jobs.state_machine import JobStatus


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class MediaJobRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                if any(error.get("loc", ())[-1:] == ("workflow",) for error in exc.errors()):
                    return _error(422, "workflow_not_allowed", "The workflow is not allowed.")
                return _error(422, "invalid_request", "The request is invalid.")
            except MediaJobsDisabledError:
                return _error(503, "service_unavailable", "Persistent media jobs are unavailable.")
            except JobNotFoundError:
                return _error(404, "job_not_found", "The requested media job was not found.")
            except JobNotCancelableError:
                return _error(409, "job_not_cancelable", "The media job cannot be cancelled.")
            except JobNotRetryableError:
                return _error(409, "job_not_retryable", "The media job cannot be retried.")
            except IdempotencyConflictError:
                return _error(409, "idempotency_conflict", "The idempotency key conflicts.")
            except (AssetNotFoundError, AssetUnavailableError, ValueError):
                return _error(422, "invalid_asset", "The input asset is invalid or unavailable.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Persistent media jobs are unavailable.")
            except Exception:
                logger.error("Unhandled persistent media-job API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(
    prefix="/media/jobs",
    tags=["persistent-media-jobs"],
    route_class=MediaJobRoute,
)


def _view(job: MediaJob) -> MediaJobResponse:
    outputs = [
        MediaJobOutput(**{key: value for key, value in item.items() if key != "relative_path"})
        for item in (job.output_metadata or [])
    ]
    error = None
    if job.error_category:
        error = MediaJobError(
            code=job.error_category,
            message="The media job did not complete successfully.",
        )
    return MediaJobResponse(
        job_id=job.job_id,
        workflow=job.workflow_type,
        status=JobStatus(job.status),
        created_at=job.created_at,
        updated_at=job.updated_at,
        cancel_requested=job.cancel_requested_at is not None,
        retry_of_job_id=job.retry_of_job_id,
        outputs=outputs,
        error=error,
    )


def _key(value: str | None) -> str:
    try:
        return validate_idempotency_key(value)
    except ValueError:
        raise RequestValidationError([]) from None


@router.post(
    "",
    response_model=MediaJobResponse,
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def create_media_job(
    request: MediaJobRequest,
    response: Response,
    service: MediaJobServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    job, created = await service.create(request, _key(idempotency_key))
    response.status_code = 201 if created else 200
    return _view(job)


@router.get("", response_model=MediaJobListResponse)
async def list_media_jobs(
    service: MediaJobServiceDep,
    status: JobStatus | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    jobs, has_more = await service.list(status=status, limit=limit, offset=offset)
    return MediaJobListResponse(
        items=[_view(job) for job in jobs],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/{job_id}", response_model=MediaJobResponse)
async def get_media_job(job_id: str, service: MediaJobServiceDep):
    return _view(await service.get(job_id))


@router.post("/{job_id}/cancel", response_model=MediaJobResponse)
async def cancel_media_job(job_id: str, service: MediaJobServiceDep):
    return _view(await service.cancel(job_id))


@router.post(
    "/{job_id}/retry",
    response_model=MediaJobResponse,
    status_code=201,
)
async def retry_media_job(
    job_id: str,
    response: Response,
    service: MediaJobServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    job, created = await service.retry(job_id, _key(idempotency_key))
    response.status_code = 201 if created else 200
    return _view(job)
