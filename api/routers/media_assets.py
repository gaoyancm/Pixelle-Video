"""Controlled upload, metadata, listing, content, and soft-delete endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, File, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import MediaAssetServiceDep
from api.schemas.media_assets import (
    AssetErrorResponse,
    MediaAssetListResponse,
    MediaAssetResponse,
)
from api.schemas.media_jobs import validate_idempotency_key
from pixelle_video.media_assets import (
    AssetIdempotencyConflictError,
    AssetKind,
    AssetNotFoundError,
    AssetState,
    AssetUnavailableError,
    ObjectTooLargeError,
    StoreBoundaryError,
    UnsupportedMediaError,
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class MediaAssetRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except AssetNotFoundError:
                return _error(404, "asset_not_found", "The requested asset was not found.")
            except AssetUnavailableError:
                return _error(409, "asset_unavailable", "The requested asset is unavailable.")
            except AssetIdempotencyConflictError:
                return _error(409, "idempotency_conflict", "The idempotency key conflicts.")
            except ObjectTooLargeError:
                return _error(413, "asset_too_large", "The uploaded asset is too large.")
            except (UnsupportedMediaError, ValueError):
                return _error(422, "invalid_asset", "The uploaded asset is invalid.")
            except (StoreBoundaryError, SQLAlchemyError):
                return _error(503, "service_unavailable", "The asset service is unavailable.")
            except Exception:
                logger.error("Unhandled media-asset API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(
    prefix="/assets",
    tags=["media-assets"],
    route_class=MediaAssetRoute,
)


def _view(asset) -> MediaAssetResponse:
    return MediaAssetResponse(
        asset_id=asset.id,
        kind=AssetKind(asset.kind),
        state=AssetState(asset.state),
        original_filename=asset.original_filename,
        media_type=asset.media_type,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        sha256=asset.sha256,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _key(value: str | None) -> str:
    try:
        return validate_idempotency_key(value)
    except ValueError:
        raise RequestValidationError([]) from None


@router.post(
    "",
    response_model=MediaAssetResponse,
    status_code=201,
    responses={
        409: {"model": AssetErrorResponse},
        413: {"model": AssetErrorResponse},
        422: {"model": AssetErrorResponse},
    },
)
async def upload_asset(
    response: Response,
    service: MediaAssetServiceDep,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    asset, created = await service.upload(
        file.file,
        filename=file.filename or "",
        mime_type=file.content_type,
        idempotency_key=_key(idempotency_key),
    )
    response.status_code = 201 if created else 200
    return _view(asset)


@router.get("", response_model=MediaAssetListResponse)
async def list_assets(
    service: MediaAssetServiceDep,
    kind: AssetKind | None = None,
    state: AssetState | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    assets, has_more = await service.list(
        kind=kind, state=state, limit=limit, offset=offset
    )
    return MediaAssetListResponse(
        items=[_view(asset) for asset in assets],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/{asset_id}", response_model=MediaAssetResponse)
async def get_asset(asset_id: str, service: MediaAssetServiceDep):
    return _view(await service.get(asset_id))


@router.get("/{asset_id}/content")
async def get_asset_content(asset_id: str, service: MediaAssetServiceDep):
    asset, stream = await service.open_content(asset_id)
    filename = asset.original_filename.replace('"', "_").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        stream,
        media_type=asset.mime_type,
        headers={
            "Content-Length": str(asset.size_bytes),
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.delete("/{asset_id}", response_model=MediaAssetResponse)
async def delete_asset(asset_id: str, service: MediaAssetServiceDep):
    return _view(await service.soft_delete(asset_id))
