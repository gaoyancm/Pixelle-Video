"""Trusted-network phase 04-E orchestration endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import OrchestrationServiceDep
from api.schemas.orchestration import (
    ApprovalSummaryResponse,
    ApproveResponse,
    GenerateResponse,
    PlanCreateRequest,
    PlanResponse,
    RejectRequest,
    StatusResponse,
)
from pixelle_video.orchestration.budget_guard import BudgetExceededError
from pixelle_video.orchestration.pipeline import PipelineError
from pixelle_video.orchestration.repository import ContentPlanNotFoundError


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _plan_view(plan) -> dict:
    return {
        "id": plan.id,
        "project_id": plan.project_id,
        "request_text": plan.request_text,
        "intent": plan.intent,
        "plan_json": plan.plan_json,
        "status": plan.status,
        "cost_estimate": plan.cost_estimate,
        "checkpoint_json": plan.checkpoint_json,
        "created_at": plan.created_at,
    }


class OrchestrationRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except ContentPlanNotFoundError:
                return _error(404, "plan_not_found", "The requested content plan was not found.")
            except BudgetExceededError as exc:
                return _error(402, "budget_exceeded", str(exc))
            except PipelineError as exc:
                return _error(409, "pipeline_failed", str(exc))
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Orchestration storage is unavailable.")
            except Exception:
                logger.error("Unhandled orchestration API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/orchestration", tags=["orchestration"], route_class=OrchestrationRoute)


@router.post("/plans", response_model=PlanResponse, status_code=201)
async def create_plan(body: PlanCreateRequest, service: OrchestrationServiceDep):
    return _plan_view(await service.create_plan(body))


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(plan_id: str, service: OrchestrationServiceDep):
    plan = await service.get_plan(plan_id)
    if plan is None:
        return _error(404, "plan_not_found", "The requested content plan was not found.")
    return _plan_view(plan)


@router.post("/plans/{plan_id}/generate", response_model=GenerateResponse)
async def generate_plan(plan_id: str, service: OrchestrationServiceDep):
    return await service.generate(plan_id)


@router.get("/plans/{plan_id}/status", response_model=StatusResponse)
async def plan_status(plan_id: str, service: OrchestrationServiceDep):
    return await service.status(plan_id)


@router.post("/plans/{plan_id}/resume", response_model=GenerateResponse)
async def resume_plan(plan_id: str, service: OrchestrationServiceDep):
    return await service.resume(plan_id)


@router.post("/plans/{plan_id}/approve", response_model=ApproveResponse)
async def approve_plan(plan_id: str, service: OrchestrationServiceDep):
    return await service.approve(plan_id)


@router.post("/plans/{plan_id}/reject", response_model=ApproveResponse)
async def reject_plan(plan_id: str, body: RejectRequest, service: OrchestrationServiceDep):
    return await service.reject(plan_id, body.reason)


@router.post("/plans/{plan_id}/retry", response_model=GenerateResponse)
async def retry_plan(plan_id: str, service: OrchestrationServiceDep):
    return await service.generate(plan_id)


@router.post("/plans/{plan_id}/stage/{stage_name}/retry", response_model=GenerateResponse)
async def retry_stage(plan_id: str, stage_name: str, service: OrchestrationServiceDep):
    return await service.retry_stage(plan_id, stage_name)


@router.get("/plans/{plan_id}/approval-summary", response_model=ApprovalSummaryResponse)
async def approval_summary(plan_id: str, service: OrchestrationServiceDep):
    return await service.approval_summary(plan_id)
