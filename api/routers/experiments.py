"""Trusted-network phase 04-C experiment endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import ExperimentServiceDep
from api.schemas.experiments import (
    ExperimentCreate,
    ExperimentDetail,
    ExperimentJobsResponse,
    ExperimentList,
    ExperimentMetricsResponse,
    ExperimentReportResponse,
    ExperimentResponse,
    ExperimentUpdate,
    FailureList,
)
from pixelle_video.experiments.repository import (
    ExperimentGroupNotFoundError,
    ExperimentNotFoundError,
)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _experiment_view(experiment) -> dict:
    return {
        "id": experiment.id,
        "name": experiment.name,
        "description": experiment.description,
        "metric": experiment.metric,
        "status": experiment.status,
        "created_at": experiment.created_at,
    }


def _group_view(group) -> dict:
    return {
        "id": group.id,
        "group_name": group.group_name,
        "prompt_version_id": group.prompt_version_id,
        "model_name": group.model_name,
        "params_json": group.params_json,
        "created_at": group.created_at,
    }


def _failure_view(sample) -> dict:
    return {
        "id": sample.id,
        "job_id": sample.job_id,
        "prompt_version_id": sample.prompt_version_id,
        "reason": sample.reason,
        "qc_issues": sample.qc_issues_json,
        "created_at": sample.created_at,
    }


class ExperimentRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except ExperimentNotFoundError:
                return _error(
                    404, "experiment_not_found", "The requested experiment was not found."
                )
            except ExperimentGroupNotFoundError:
                return _error(422, "group_not_found", "The experiment group was not found.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Experiment storage is unavailable.")
            except Exception:
                logger.error("Unhandled experiment API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/admin/experiments", tags=["experiments"], route_class=ExperimentRoute)


@router.post("", response_model=ExperimentResponse, status_code=201)
async def create_experiment(body: ExperimentCreate, service: ExperimentServiceDep):
    return _experiment_view(await service.create(body))


@router.get("", response_model=ExperimentList)
async def list_experiments(
    service: ExperimentServiceDep,
    status: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list(status=status, limit=limit + 1, offset=offset)
    return ExperimentList(items=[_experiment_view(row) for row in rows[:limit]], has_more=has_more)


@router.get("/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(experiment_id: str, service: ExperimentServiceDep):
    experiment = await service.get(experiment_id)
    if experiment is None:
        return _error(404, "experiment_not_found", "The requested experiment was not found.")
    groups = await service.groups(experiment_id)
    detail = _experiment_view(experiment)
    detail["groups"] = [_group_view(group) for group in groups]
    return ExperimentDetail(**detail)


@router.patch("/{experiment_id}", response_model=ExperimentResponse)
async def update_experiment(
    experiment_id: str, body: ExperimentUpdate, service: ExperimentServiceDep
):
    return _experiment_view(await service.update_status(experiment_id, body.status))


@router.get("/{experiment_id}/metrics", response_model=ExperimentMetricsResponse)
async def experiment_metrics(experiment_id: str, service: ExperimentServiceDep):
    payload = await service.metrics(experiment_id)
    return ExperimentMetricsResponse(**payload)


@router.get("/{experiment_id}/jobs", response_model=ExperimentJobsResponse)
async def experiment_jobs(experiment_id: str, service: ExperimentServiceDep):
    items = await service.jobs(experiment_id)
    return ExperimentJobsResponse(experiment_id=experiment_id, items=items)


@router.get("/{experiment_id}/report", response_model=ExperimentReportResponse)
async def experiment_report(experiment_id: str, service: ExperimentServiceDep):
    payload = await service.report(experiment_id)
    return ExperimentReportResponse(**payload)


@router.get("/{experiment_id}/failures", response_model=FailureList)
async def experiment_failures(experiment_id: str, service: ExperimentServiceDep):
    samples = await service.collect_failures(experiment_id)
    return FailureList(items=[_failure_view(sample) for sample in samples])
