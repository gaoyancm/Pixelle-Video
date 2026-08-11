"""Trusted-network phase 06 short-video pipeline endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import VideoServiceDep
from api.schemas.videos import (
    AssetProgressResponse,
    ComposeResponse,
    ComposeStatusResponse,
    ConfirmResponse,
    GenerateAssetsResponse,
    ScriptGenerateRequest,
    ScriptResponse,
    ScriptUpdateRequest,
    StoryboardResponse,
    VideoPackageResponse,
)
from pixelle_video.videos.repository import VideoScriptNotFoundError


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _script_view(script) -> dict:
    return {
        "id": script.id,
        "project_id": script.project_id,
        "topic": script.topic,
        "language": script.language,
        "target_duration": script.target_duration,
        "platform": script.platform,
        "script_json": script.script_json,
        "prompt_version_id": script.prompt_version_id,
        "status": script.status,
        "created_at": script.created_at,
        "updated_at": script.updated_at,
    }


class VideoRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except VideoScriptNotFoundError:
                return _error(404, "script_not_found", "The requested video script was not found.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Video storage is unavailable.")
            except Exception:
                logger.error("Unhandled video API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/videos", tags=["videos"], route_class=VideoRoute)


@router.post("/scripts/generate", response_model=ScriptResponse, status_code=201)
async def generate_script(body: ScriptGenerateRequest, service: VideoServiceDep):
    payload = await service.generate_script(body)
    script = await service.get_script(payload["id"])
    return _script_view(script)


@router.get("/scripts/{script_id}", response_model=ScriptResponse)
async def get_script(script_id: str, service: VideoServiceDep):
    script = await service.get_script(script_id)
    if script is None:
        return _error(404, "script_not_found", "The requested video script was not found.")
    return _script_view(script)


@router.patch("/scripts/{script_id}", response_model=ScriptResponse)
async def update_script(script_id: str, body: ScriptUpdateRequest, service: VideoServiceDep):
    return _script_view(await service.update_script(script_id, body))


@router.post("/scripts/{script_id}/confirm", response_model=ConfirmResponse)
async def confirm_script(script_id: str, service: VideoServiceDep):
    return await service.confirm(script_id)


@router.post("/scripts/from-plan/{plan_id}", status_code=201)
async def create_script_from_plan(plan_id: str, service: VideoServiceDep):
    return await service.create_script_from_plan(plan_id)


@router.get("/scripts/{script_id}/plan")
async def get_plan_for_script(script_id: str, service: VideoServiceDep):
    result = await service.get_plan_for_script(script_id)
    if result is None:
        from api.routers.videos import _error

        return _error(404, "plan_link_missing", "No linked content plan for this script.")
    return result


@router.post("/scripts/{script_id}/confirm-from-plan", response_model=StoryboardResponse)
async def confirm_from_plan(script_id: str, service: VideoServiceDep):
    return await service.confirm_from_plan(script_id)


@router.post("/scripts/{script_id}/storyboard", response_model=StoryboardResponse)
async def build_storyboard(script_id: str, service: VideoServiceDep):
    return await service.build_storyboard(script_id)


@router.get("/scripts/{script_id}/storyboard", response_model=StoryboardResponse)
async def get_storyboard(script_id: str, service: VideoServiceDep):
    return await service.get_storyboard(script_id)


@router.post("/scripts/{script_id}/generate-assets", response_model=GenerateAssetsResponse)
async def generate_assets(script_id: str, service: VideoServiceDep):
    return await service.generate_assets(script_id)


@router.get("/scripts/{script_id}/assets/progress", response_model=AssetProgressResponse)
async def asset_progress(script_id: str, service: VideoServiceDep):
    return await service.asset_progress(script_id)


@router.post("/scripts/{script_id}/compose", response_model=ComposeResponse)
async def compose(script_id: str, service: VideoServiceDep):
    return await service.compose(script_id)


@router.get("/scripts/{script_id}/compose/status", response_model=ComposeStatusResponse)
async def compose_status(script_id: str, service: VideoServiceDep):
    return await service.compose_status(script_id)


@router.get("/scripts/{script_id}/result")
async def video_result(script_id: str, service: VideoServiceDep):
    script = await service.get_script(script_id)
    if script is None:
        return _error(404, "script_not_found", "The requested video script was not found.")
    path = service.result_path(script_id)
    if path is None:
        return _error(404, "video_missing", "No composed video exists yet.")
    return FileResponse(path, media_type="video/mp4", filename=f"{script_id}.mp4")


@router.post("/scripts/{script_id}/package", response_model=VideoPackageResponse)
async def package_video(
    script_id: str,
    service: VideoServiceDep,
    platforms: list[str] = Query(default=["tiktok", "youtube_shorts"]),
):
    return await service.package(script_id, platforms)


@router.get("/scripts/{script_id}/download")
async def download_package(script_id: str, service: VideoServiceDep):
    script = await service.get_script(script_id)
    if script is None:
        return _error(404, "script_not_found", "The requested video script was not found.")
    zip_path = service.download_zip(script_id, script.project_id)
    if zip_path is None:
        return _error(404, "package_missing", "No video package exists yet.")
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"{script_id}_video_package.zip"
    )
