"""Trusted-network phase 03-F audit and budget endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import AuditServiceDep, BudgetServiceDep
from api.schemas.audit_budget import (
    AuditListResponse,
    BudgetConfigResponse,
    BudgetConfigUpdate,
    BudgetUsageResponse,
)
from pixelle_video.budget import BudgetBlockedError, BudgetConfigurationError


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


class AuditBudgetRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except BudgetBlockedError as exc:
                return _error(
                    429,
                    "budget_limit_exceeded",
                    str(exc),
                    limit=exc.limit,
                    estimated=exc.estimated,
                )
            except BudgetConfigurationError:
                return _error(422, "invalid_budget_config", "The budget configuration is invalid.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Audit or budget storage is unavailable.")
            except Exception:
                logger.error("Unhandled audit/budget API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/admin", tags=["audit-budget"], route_class=AuditBudgetRoute)


def _audit_item(event) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "scope_type": event.scope_type,
        "scope_id": event.scope_id,
        "operator": event.operator,
        "details": event.details_json,
        "cost_snapshot": event.cost_snapshot,
        "created_at": event.created_at,
    }


@router.get("/audit", response_model=AuditListResponse)
async def list_audit(
    service: AuditServiceDep,
    scope_type: str | None = None,
    scope_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    if (scope_type is None) != (scope_id is None):
        raise RequestValidationError([])
    rows, has_more = await service.list(
        scope_type=scope_type,
        scope_id=scope_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
    return AuditListResponse(
        items=[_audit_item(row) for row in rows], limit=limit, offset=offset, has_more=has_more
    )


@router.get("/audit/{event_id}")
async def get_audit(event_id: str, service: AuditServiceDep):
    event = await service.get(event_id)
    if event is None:
        return _error(404, "not_found", "The requested audit event was not found.")
    return _audit_item(event)


@router.get("/budget/config", response_model=BudgetConfigResponse)
async def get_budget_config(service: BudgetServiceDep):
    config = await service.get_config()
    return BudgetConfigResponse(
        per_task_limit=config.per_task_limit,
        per_batch_limit=config.per_batch_limit,
        mode=config.mode,
    )


@router.patch("/budget/config", response_model=BudgetConfigResponse)
async def update_budget_config(body: BudgetConfigUpdate, service: BudgetServiceDep):
    config = await service.update_config(
        per_task_limit=body.per_task_limit,
        per_batch_limit=body.per_batch_limit,
        mode=body.mode,
    )
    return BudgetConfigResponse(
        per_task_limit=config.per_task_limit,
        per_batch_limit=config.per_batch_limit,
        mode=config.mode,
    )


@router.get("/budget/usage", response_model=BudgetUsageResponse)
async def budget_usage(
    service: BudgetServiceDep,
    project_id: str = Query(min_length=1, max_length=128),
):
    return await service.usage(project_id)
