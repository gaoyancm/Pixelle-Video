"""Trusted-network phase 04-A prompt template endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import PromptServiceDep
from api.schemas.prompts import (
    AntiSlopResponse,
    BindTagsRequest,
    CompileRequest,
    CompileResponse,
    PromptCreate,
    PromptList,
    PromptResponse,
    PromptUpdate,
    RateRequest,
    RollbackRequest,
    TagList,
    ValidateResponse,
    VersionList,
)
from pixelle_video.prompts.compiler import PromptCompileError
from pixelle_video.prompts.repository import (
    PromptNameConflictError,
    PromptNotFoundError,
    PromptTagNotFoundError,
    PromptVersionNotFoundError,
)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _view(template) -> dict:
    return {
        "id": template.id,
        "name": template.name,
        "category": template.category,
        "description": template.description,
        "template_text": template.template_text,
        "variables": template.variables_json or [],
        "provider": template.provider,
        "is_active": template.is_active == 1,
        "current_score": template.current_score,
        "usage_count": template.usage_count,
        "last_used_at": template.last_used_at,
        "archived": template.archived_at is not None,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


class PromptRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except PromptNotFoundError:
                return _error(404, "not_found", "The requested prompt template was not found.")
            except PromptVersionNotFoundError:
                return _error(404, "version_not_found", "The requested version was not found.")
            except PromptNameConflictError:
                return _error(
                    409, "name_conflict", "A prompt template with this name already exists."
                )
            except PromptTagNotFoundError:
                return _error(422, "tag_not_found", "One of the requested tags was not found.")
            except (PromptCompileError, ValueError):
                return _error(422, "compile_failed", "The prompt could not be compiled.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Prompt storage is unavailable.")
            except Exception:
                logger.error("Unhandled prompt API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/admin/prompts", tags=["prompt-templates"], route_class=PromptRoute)


@router.post("", response_model=PromptResponse, status_code=201)
async def create_prompt(body: PromptCreate, service: PromptServiceDep):
    return _view(await service.create(body))


@router.get("", response_model=PromptList)
async def list_prompts(
    service: PromptServiceDep,
    category: str | None = None,
    is_active: bool | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list(
        category=category, is_active=is_active, limit=limit + 1, offset=offset
    )
    return PromptList(
        items=[_view(row) for row in rows[:limit]], limit=limit, offset=offset, has_more=has_more
    )


@router.get("/search", response_model=PromptList)
async def search_prompts(
    service: PromptServiceDep,
    tag: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.search(
        tag=tag, category=category, q=q, limit=limit + 1, offset=offset
    )
    return PromptList(
        items=[_view(row) for row in rows[:limit]], limit=limit, offset=offset, has_more=has_more
    )


@router.get("/tags", response_model=TagList)
async def list_tags(service: PromptServiceDep):
    return TagList(items=[{"id": tag.id, "name": tag.name} for tag in await service.list_tags()])


@router.get("/categories", response_model=list[str])
async def list_categories(service: PromptServiceDep):
    return await service.categories()


@router.get("/{template_id}", response_model=PromptResponse)
async def get_prompt(template_id: str, service: PromptServiceDep):
    return _view(await service.get(template_id))


@router.patch("/{template_id}", response_model=PromptResponse)
async def update_prompt(template_id: str, body: PromptUpdate, service: PromptServiceDep):
    return _view(await service.update(template_id, body))


@router.post("/{template_id}/archive", response_model=PromptResponse)
async def archive_prompt(template_id: str, service: PromptServiceDep):
    return _view(await service.archive(template_id))


@router.post("/{template_id}/compile", response_model=CompileResponse)
async def compile_prompt(template_id: str, body: CompileRequest, service: PromptServiceDep):
    result = await service.compile_prompt(template_id, body.variables)
    return CompileResponse(**result)


@router.post("/{template_id}/validate", response_model=ValidateResponse)
async def validate_prompt(template_id: str, body: CompileRequest, service: PromptServiceDep):
    result = await service.validate_prompt(template_id, body.variables)
    return ValidateResponse(**result)


@router.get("/{template_id}/versions", response_model=VersionList)
async def list_versions(template_id: str, service: PromptServiceDep):
    rows = await service.versions(template_id)
    return VersionList(
        template_id=template_id,
        items=[
            {
                "id": row.id,
                "version_no": row.version_no,
                "template_text": row.template_text,
                "variables": row.variables_json or [],
                "change_note": row.change_note,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    )


@router.post("/{template_id}/rollback", response_model=PromptResponse)
async def rollback_prompt(
    template_id: str,
    service: PromptServiceDep,
    version: int = Query(ge=1),
    body: RollbackRequest | None = None,
):
    change_note = body.change_note if body is not None else None
    return _view(await service.rollback(template_id, version, change_note=change_note))


@router.post("/{template_id}/rate", response_model=PromptResponse)
async def rate_prompt(template_id: str, body: RateRequest, service: PromptServiceDep):
    return _view(await service.rate(template_id, body.score, comment=body.comment))


@router.post("/{template_id}/tags", response_model=TagList)
async def bind_tags(template_id: str, body: BindTagsRequest, service: PromptServiceDep):
    rows = await service.bind_tags(template_id, body.tag_ids)
    return TagList(items=[{"id": tag.id, "name": tag.name} for tag in rows])


@router.post("/{template_id}/check-quality", response_model=AntiSlopResponse)
async def check_quality(template_id: str, service: PromptServiceDep):
    report = await service.check_quality(template_id)
    return AntiSlopResponse(**report)
