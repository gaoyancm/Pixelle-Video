"""Trusted-network phase 04-B QC pipeline endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import QCServiceDep
from api.schemas.qc import (
    QCDiagnoseRequest,
    QCDiagnosisResponse,
    QCProfileCreate,
    QCProfileList,
    QCProfileResponse,
    QCReportResponse,
    QCResultResponse,
    QCRuleCreate,
    QCRuleList,
    QCRuleResponse,
    QCRuleUpdate,
)
from pixelle_video.qc.executor import QCJobNotFoundError, QCRunError
from pixelle_video.qc.repository import (
    QCProfileNameConflictError,
    QCProfileNotFoundError,
    QCRuleNameConflictError,
    QCRuleNotFoundError,
)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _rule_view(rule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "category": rule.category,
        "rule_type": rule.rule_type,
        "rule_config": rule.rule_config_json,
        "provider": rule.provider,
        "is_active": rule.is_active == 1,
        "priority": rule.priority,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _profile_view(profile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "rules": profile.rules_json or [],
        "is_default": profile.is_default == 1,
        "created_at": profile.created_at,
    }


class QCRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except QCRuleNotFoundError:
                return _error(404, "rule_not_found", "The requested QC rule was not found.")
            except QCProfileNotFoundError:
                return _error(404, "profile_not_found", "The requested QC profile was not found.")
            except QCJobNotFoundError:
                return _error(404, "job_not_found", "The requested job was not found.")
            except (QCRuleNameConflictError, QCProfileNameConflictError):
                return _error(
                    409, "name_conflict", "A QC rule or profile with this name already exists."
                )
            except QCRunError as exc:
                return _error(422, "qc_run_failed", str(exc))
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "QC storage is unavailable.")
            except Exception:
                logger.error("Unhandled QC API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/admin/qc", tags=["qc-pipeline"], route_class=QCRoute)


@router.post("/rules", response_model=QCRuleResponse, status_code=201)
async def create_rule(body: QCRuleCreate, service: QCServiceDep):
    return _rule_view(await service.create_rule(body))


@router.get("/rules", response_model=QCRuleList)
async def list_rules(
    service: QCServiceDep,
    category: str | None = None,
    is_active: bool | None = None,
):
    rows = await service.list_rules(category=category, is_active=is_active)
    return QCRuleList(items=[_rule_view(row) for row in rows])


@router.patch("/rules/{rule_id}", response_model=QCRuleResponse)
async def update_rule(rule_id: str, body: QCRuleUpdate, service: QCServiceDep):
    return _rule_view(await service.update_rule(rule_id, body))


@router.post("/profiles", response_model=QCProfileResponse, status_code=201)
async def create_profile(body: QCProfileCreate, service: QCServiceDep):
    return _profile_view(await service.create_profile(body))


@router.get("/profiles", response_model=QCProfileList)
async def list_profiles(service: QCServiceDep):
    rows = await service.list_profiles()
    return QCProfileList(items=[_profile_view(row) for row in rows])


@router.post("/run/{job_id}", response_model=QCResultResponse)
async def run_qc(
    job_id: str,
    service: QCServiceDep,
    profile: str | None = None,
):
    payload = await service.run_qc(job_id, profile_id=profile)
    return QCResultResponse(**payload)


@router.get("/results/{job_id}", response_model=QCResultResponse)
async def get_result(job_id: str, service: QCServiceDep):
    payload = await service.get_result(job_id)
    if payload is None:
        return _error(404, "result_not_found", "No QC result exists for this job yet.")
    return QCResultResponse(**payload)


@router.get("/report/{job_id}", response_model=QCReportResponse)
async def qc_report(job_id: str, service: QCServiceDep):
    payload = await service.report(job_id)
    return QCReportResponse(**payload)


@router.post("/diagnose", response_model=QCDiagnosisResponse)
async def diagnose(body: QCDiagnoseRequest, service: QCServiceDep):
    payload = await service.diagnose(body.issue_type, body.context)
    return QCDiagnosisResponse(**payload)
