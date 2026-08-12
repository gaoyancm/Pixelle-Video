"""Trusted-network phase 05 product & ad pipeline endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import ProductServiceDep
from api.schemas.products import (
    AdaptRequest,
    AdaptResponse,
    ConfirmResponse,
    GenerateIdeasResponse,
    PackageRequest,
    PackageResponse,
    PackageStatusResponse,
    ProductBriefCreate,
    ProductBriefList,
    ProductBriefResponse,
    ProductBriefUpdate,
    ProgressResponse,
    ResultsResponse,
)
from pixelle_video.media_assets.service import AssetNotFoundError
from pixelle_video.products.platform_adapter import PlatformNotFoundError
from pixelle_video.products.repository import ProductBriefNotFoundError


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _brief_view(brief) -> dict:
    return {
        "id": brief.id,
        "project_id": brief.project_id,
        "product_name": brief.product_name,
        "category": brief.category,
        "description": brief.description,
        "selling_points": brief.selling_points_json,
        "target_audience": brief.target_audience,
        "brand_profile_id": brief.brand_profile_id,
        "platforms": brief.platforms_json,
        "reference_images": brief.reference_images_json,
        "status": brief.status,
        "created_at": brief.created_at,
        "updated_at": brief.updated_at,
    }


class ProductRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except ProductBriefNotFoundError:
                return _error(404, "brief_not_found", "The requested product brief was not found.")
            except AssetNotFoundError:
                return _error(404, "asset_not_found", "The requested asset was not found.")
            except PlatformNotFoundError as exc:
                return _error(422, "platform_not_supported", str(exc))
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Product storage is unavailable.")
            except Exception:
                import traceback; traceback.print_exc()
                logger.error("Unhandled product API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/products", tags=["products"], route_class=ProductRoute)


@router.post("/briefs", response_model=ProductBriefResponse, status_code=201)
async def create_brief(body: ProductBriefCreate, service: ProductServiceDep):
    return _brief_view(await service.create(body))


@router.get("/briefs", response_model=ProductBriefList)
async def list_briefs(
    service: ProductServiceDep,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list(
        project_id=project_id, status=status, limit=limit + 1, offset=offset
    )
    return ProductBriefList(items=[_brief_view(row) for row in rows[:limit]], has_more=has_more)


@router.get("/briefs/{brief_id}", response_model=ProductBriefResponse)
async def get_brief(brief_id: str, service: ProductServiceDep):
    brief = await service.get(brief_id)
    if brief is None:
        return _error(404, "brief_not_found", "The requested product brief was not found.")
    return _brief_view(brief)


@router.patch("/briefs/{brief_id}", response_model=ProductBriefResponse)
async def update_brief(brief_id: str, body: ProductBriefUpdate, service: ProductServiceDep):
    return _brief_view(await service.update(brief_id, body))


@router.post("/briefs/{brief_id}/generate-ideas", response_model=GenerateIdeasResponse)
async def generate_ideas(brief_id: str, service: ProductServiceDep):
    return await service.generate_ideas(brief_id)


@router.post("/briefs/from-plan/{plan_id}", status_code=201)
async def create_brief_from_plan(plan_id: str, service: ProductServiceDep):
    return await service.create_brief_from_plan(plan_id)


@router.get("/briefs/{brief_id}/plan")
async def get_plan_for_brief(brief_id: str, service: ProductServiceDep):
    result = await service.get_plan_for_brief(brief_id)
    if result is None:
        from api.routers.products import _error

        return _error(404, "plan_link_missing", "No linked content plan for this brief.")
    return result


@router.post("/briefs/{brief_id}/confirm-from-plan", response_model=ConfirmResponse)
async def confirm_from_plan(brief_id: str, service: ProductServiceDep):
    return await service.confirm_from_plan(brief_id)


@router.post("/briefs/{brief_id}/confirm", response_model=ConfirmResponse)
async def confirm_brief(brief_id: str, service: ProductServiceDep):
    return await service.confirm(brief_id)


@router.get("/briefs/{brief_id}/progress", response_model=ProgressResponse)
async def brief_progress(brief_id: str, service: ProductServiceDep):
    return await service.progress(brief_id)


@router.get("/briefs/{brief_id}/results", response_model=ResultsResponse)
async def brief_results(brief_id: str, service: ProductServiceDep):
    return await service.results(brief_id)


@router.post("/assets/{asset_id}/adapt", response_model=AdaptResponse)
async def adapt_asset(asset_id: str, body: AdaptRequest, service: ProductServiceDep):
    return await service.adapt_asset(asset_id, body.platform, body.output_dir)


@router.post("/briefs/{brief_id}/package", response_model=PackageResponse)
async def package_brief(brief_id: str, body: PackageRequest, service: ProductServiceDep):
    return await service.package(brief_id, body.platforms)


@router.get("/briefs/{brief_id}/package/status", response_model=PackageStatusResponse)
async def package_status(brief_id: str, service: ProductServiceDep):
    return await service.package_status(brief_id)


@router.get("/briefs/{brief_id}/download")
async def download_brief(brief_id: str, service: ProductServiceDep):
    brief = await service.get(brief_id)
    if brief is None:
        return _error(404, "brief_not_found", "The requested product brief was not found.")
    zip_path = service.download_zip(brief_id, brief.project_id)
    if zip_path is None:
        return _error(404, "package_missing", "No delivery package has been built yet.")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{brief_id}_delivery.zip")
