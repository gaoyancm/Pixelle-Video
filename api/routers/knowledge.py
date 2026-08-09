"""Trusted-network phase 04-D knowledge base endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import KnowledgeServiceDep
from api.schemas.knowledge import (
    KnowledgeEntryCreate,
    KnowledgeEntryList,
    KnowledgeEntryResponse,
    KnowledgeEntryUpdate,
    KnowledgeLinkCreate,
    KnowledgeLinkList,
    KnowledgeLinkResponse,
)
from pixelle_video.knowledge.repository import (
    KnowledgeEntryNotFoundError,
    KnowledgeValidationError,
)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _entry_view(entry, tags: list[str]) -> dict:
    return {
        "id": entry.id,
        "title": entry.title,
        "content": entry.content,
        "category": entry.category,
        "evidence_class": entry.evidence_class,
        "status": entry.status,
        "source_url": entry.source_url,
        "source_doc": entry.source_doc,
        "verified_at": entry.verified_at,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "stale": _is_stale(entry),
        "tags": tags,
    }


def _is_stale(entry) -> bool:
    from pixelle_video.knowledge.repository import KnowledgeRepository

    return KnowledgeRepository.is_stale(entry)


class KnowledgeRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except KnowledgeEntryNotFoundError:
                return _error(
                    404, "entry_not_found", "The requested knowledge entry was not found."
                )
            except KnowledgeValidationError as exc:
                return _error(422, "validation_failed", str(exc))
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Knowledge storage is unavailable.")
            except Exception:
                logger.error("Unhandled knowledge API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/admin/knowledge", tags=["knowledge"], route_class=KnowledgeRoute)


@router.get("/stale", response_model=KnowledgeEntryList)
async def stale_entries(service: KnowledgeServiceDep):
    rows = await service.stale()
    items = [
        _entry_view(entry, [tag.name for tag in await service.tags(entry.id)]) for entry in rows
    ]
    return KnowledgeEntryList(items=items, has_more=False)


@router.get("/search", response_model=KnowledgeEntryList)
async def search_entries(
    service: KnowledgeServiceDep,
    q: str | None = None,
    tag: str | None = None,
    category: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.search(
        query=q, tag=tag, category=category, limit=limit + 1, offset=offset
    )
    items = [
        _entry_view(entry, [tag.name for tag in await service.tags(entry.id)])
        for entry in rows[:limit]
    ]
    return KnowledgeEntryList(items=items, has_more=has_more)


@router.post("", response_model=KnowledgeEntryResponse, status_code=201)
async def create_entry(body: KnowledgeEntryCreate, service: KnowledgeServiceDep):
    entry = await service.create(body)
    return _entry_view(entry, body.tags)


@router.get("", response_model=KnowledgeEntryList)
async def list_entries(
    service: KnowledgeServiceDep,
    category: str | None = None,
    status: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list(
        category=category, status=status, limit=limit + 1, offset=offset
    )
    items = [
        _entry_view(entry, [tag.name for tag in await service.tags(entry.id)])
        for entry in rows[:limit]
    ]
    return KnowledgeEntryList(items=items, has_more=has_more)


@router.get("/{entry_id}", response_model=KnowledgeEntryResponse)
async def get_entry(entry_id: str, service: KnowledgeServiceDep):
    entry = await service.get(entry_id)
    if entry is None:
        return _error(404, "entry_not_found", "The requested knowledge entry was not found.")
    return _entry_view(entry, [tag.name for tag in await service.tags(entry_id)])


@router.patch("/{entry_id}", response_model=KnowledgeEntryResponse)
async def update_entry(entry_id: str, body: KnowledgeEntryUpdate, service: KnowledgeServiceDep):
    entry = await service.update(entry_id, body)
    return _entry_view(entry, [tag.name for tag in await service.tags(entry_id)])


@router.post("/{entry_id}/archive", response_model=KnowledgeEntryResponse)
async def archive_entry(entry_id: str, service: KnowledgeServiceDep):
    entry = await service.archive(entry_id)
    return _entry_view(entry, [tag.name for tag in await service.tags(entry_id)])


@router.post("/{entry_id}/verify", response_model=KnowledgeEntryResponse)
async def verify_entry(entry_id: str, service: KnowledgeServiceDep):
    entry = await service.verify(entry_id)
    return _entry_view(entry, [tag.name for tag in await service.tags(entry_id)])


@router.post("/{entry_id}/link", response_model=KnowledgeLinkResponse, status_code=201)
async def add_link(entry_id: str, body: KnowledgeLinkCreate, service: KnowledgeServiceDep):
    link = await service.add_link(entry_id, body)
    return KnowledgeLinkResponse(
        id=link.id,
        entry_id=link.entry_id,
        target_type=link.target_type,
        target_id=link.target_id,
        link_note=link.link_note,
        created_at=link.created_at,
    )


@router.get("/{entry_id}/links", response_model=KnowledgeLinkList)
async def list_links(entry_id: str, service: KnowledgeServiceDep):
    links = await service.links(entry_id)
    return KnowledgeLinkList(
        items=[
            KnowledgeLinkResponse(
                id=link.id,
                entry_id=link.entry_id,
                target_type=link.target_type,
                target_id=link.target_id,
                link_note=link.link_note,
                created_at=link.created_at,
            )
            for link in links
        ]
    )
