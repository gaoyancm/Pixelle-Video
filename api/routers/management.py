"""Trusted-network phase 03 management endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import ManagementServiceDep
from api.schemas.management import (
    BatchCreate,
    BatchList,
    BatchResponse,
    BatchUpdate,
    DraftItemsReplace,
    DraftItemsResponse,
    PreflightResponse,
    ProjectCreate,
    ProjectList,
    ProjectResponse,
    ProjectUpdate,
    SubmitResponse,
    VersionRequest,
    WorkflowCatalog,
)
from api.schemas.media_jobs import validate_idempotency_key
from api.services.management import PreflightFailedError
from pixelle_video.management import (
    ManagementConflictError,
    ManagementConstraintError,
    ManagementNotFoundError,
    SubmissionIndeterminateError,
    normalize_priority,
)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


class ManagementRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except ManagementNotFoundError:
                return _error(404, "not_found", "The requested management resource was not found.")
            except PreflightFailedError as exc:
                return _error(
                    422,
                    "preflight_failed",
                    "Batch preflight failed.",
                    issues=exc.issues,
                )
            except SubmissionIndeterminateError:
                return _error(
                    503,
                    "submission_indeterminate",
                    "The batch submission outcome could not be determined safely.",
                )
            except ManagementConflictError as exc:
                code = (
                    "idempotency_conflict"
                    if "operation key" in str(exc)
                    else "version_conflict"
                    if "version" in str(exc)
                    else "state_conflict"
                )
                return _error(409, code, "The requested management change conflicts.")
            except (ManagementConstraintError, ValueError):
                return _error(422, "invalid_request", "The request is invalid.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Management storage is unavailable.")
            except Exception:
                logger.error("Unhandled management API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/admin", tags=["management"], route_class=ManagementRoute)


def _project(row) -> ProjectResponse:
    return ProjectResponse(
        project_id=row.id,
        name=row.name,
        description=row.description,
        archived=row.archived_at is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _batch(row, service) -> BatchResponse:
    return BatchResponse(
        batch_id=row.id,
        project_id=row.project_id,
        name=row.name,
        workflow=row.workflow_type,
        state=row.state,
        common_parameters=row.common_parameters_json,
        default_priority=service.priority_name(row.default_priority),
        item_limit_snapshot=row.item_limit_snapshot,
        version=row.version,
        archived=row.archived_at is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(body: ProjectCreate, service: ManagementServiceDep):
    return _project(await service.create_project(name=body.name, description=body.description))


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    service: ManagementServiceDep,
    include_archived: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list_projects(
        include_archived=include_archived, limit=limit, offset=offset
    )
    return ProjectList(
        items=[_project(row) for row in rows], limit=limit, offset=offset, has_more=has_more
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, service: ManagementServiceDep):
    return _project(await service.get_project(project_id))


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, body: ProjectUpdate, service: ManagementServiceDep):
    return _project(
        await service.update_project(project_id, name=body.name, description=body.description)
    )


@router.post("/projects/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(project_id: str, service: ManagementServiceDep):
    return _project(await service.archive_project(project_id))


@router.post("/projects/{project_id}/batches", response_model=BatchResponse, status_code=201)
async def create_batch(project_id: str, body: BatchCreate, service: ManagementServiceDep):
    row = await service.create_batch(
        project_id,
        name=body.name,
        workflow_type=body.workflow,
        common_parameters=body.common_parameters,
        default_priority=int(normalize_priority(body.default_priority)),
    )
    return _batch(row, service)


@router.get("/projects/{project_id}/batches", response_model=BatchList)
async def list_batches(
    project_id: str,
    service: ManagementServiceDep,
    include_archived: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list_batches(
        project_id, include_archived=include_archived, limit=limit, offset=offset
    )
    return BatchList(
        items=[_batch(row, service) for row in rows],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/batches/{batch_id}", response_model=BatchResponse)
async def get_batch(batch_id: str, service: ManagementServiceDep):
    return _batch(await service.get_batch(batch_id), service)


@router.patch("/batches/{batch_id}", response_model=BatchResponse)
async def update_batch(batch_id: str, body: BatchUpdate, service: ManagementServiceDep):
    row = await service.update_batch(
        batch_id,
        name=body.name,
        workflow_type=body.workflow,
        common_parameters=body.common_parameters,
        default_priority=int(normalize_priority(body.default_priority)),
        expected_version=body.expected_batch_version,
    )
    return _batch(row, service)


@router.post("/batches/{batch_id}/archive", response_model=BatchResponse)
async def archive_batch(batch_id: str, service: ManagementServiceDep):
    return _batch(await service.archive_batch(batch_id), service)


@router.put("/batches/{batch_id}/items", response_model=DraftItemsResponse)
async def replace_items(batch_id: str, body: DraftItemsReplace, service: ManagementServiceDep):
    _rows, version = await service.replace_items(
        batch_id, expected_version=body.expected_batch_version, items=body.items
    )
    return DraftItemsResponse(
        batch_id=batch_id,
        batch_version=version,
        items=await service.item_payloads(batch_id),
    )


@router.post("/batches/{batch_id}/preflight", response_model=PreflightResponse)
async def preflight(batch_id: str, body: VersionRequest, service: ManagementServiceDep):
    prepared = await service.preflight(batch_id, expected_version=body.expected_batch_version)
    return PreflightResponse(
        batch_id=batch_id,
        batch_version=prepared.batch.version,
        valid=not prepared.issues,
        item_count=len(prepared.items),
        issues=list(prepared.issues),
    )


@router.post("/batches/{batch_id}/submit", response_model=SubmitResponse, status_code=201)
async def submit(
    batch_id: str,
    body: VersionRequest,
    response: Response,
    service: ManagementServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError:
        raise RequestValidationError([]) from None
    result, created = await service.submit(
        batch_id, expected_version=body.expected_batch_version, idempotency_key=key
    )
    response.status_code = 201 if created else 200
    return SubmitResponse.model_validate(result)


@router.get("/workflows", response_model=WorkflowCatalog)
async def workflows(service: ManagementServiceDep):
    return WorkflowCatalog(items=service.workflow_catalog())
