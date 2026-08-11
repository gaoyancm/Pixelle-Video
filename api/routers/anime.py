"""Trusted-network phase 07 anime pipeline endpoints."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import AnimeServiceDep
from api.schemas.anime import (
    AnimeProjectCreate,
    CharacterCreate,
    CharacterResponse,
    CharacterUpdate,
    ConsistencyReportResponse,
    EpisodeCreate,
    PropCreate,
    SceneAssetCreate,
    SceneGenerateResponse,
    SceneInEpisodeCreate,
    ShotCreate,
    ShotGenerateResponse,
    ShotPlanResponse,
    ShotProgressResponse,
)
from pixelle_video.anime.repository import AnimeNotFoundError


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    detail = {"code": code, "message": message, **extra}
    return JSONResponse(status_code=status, content={"error": detail})


def _character_view(character) -> dict:
    return {
        "id": character.id,
        "project_id": character.project_id,
        "name": character.name,
        "role_type": character.role_type,
        "description": character.description,
        "identity_anchors": character.identity_anchors_json,
        "static_features": character.static_features_json,
        "dynamic_features": character.dynamic_features_json,
        "reference_images": character.reference_images_json,
        "voice_config": character.voice_config_json,
        "created_at": character.created_at,
    }


def _scene_view(scene) -> dict:
    return {
        "id": scene.id,
        "project_id": scene.project_id,
        "name": scene.name,
        "description": scene.description,
        "environment": scene.environment_json,
        "reference_images": scene.reference_images_json,
        "created_at": scene.created_at,
    }


class AnimeRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "The request is invalid.")
            except AnimeNotFoundError:
                return _error(404, "anime_not_found", "The requested anime entity was not found.")
            except SQLAlchemyError:
                return _error(503, "service_unavailable", "Anime storage is unavailable.")
            except Exception:
                logger.error("Unhandled anime API error")
                return _error(500, "internal_error", "An internal error occurred.")

        return handler


router = APIRouter(prefix="/anime", tags=["anime"], route_class=AnimeRoute)


# --- C1 assets -------------------------------------------------------------------


@router.post("/characters", response_model=CharacterResponse, status_code=201)
async def create_character(body: CharacterCreate, service: AnimeServiceDep):
    return _character_view(await service.create_character(body))


@router.get("/projects/{project_id}/characters")
async def list_characters(
    project_id: str,
    service: AnimeServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, has_more = await service.list_characters(project_id, limit, offset)
    return {
        "items": [_character_view(row) for row in rows[:limit]],
        "has_more": has_more,
    }


@router.patch("/characters/{character_id}", response_model=CharacterResponse)
async def update_character(character_id: str, body: CharacterUpdate, service: AnimeServiceDep):
    return _character_view(await service.update_character(character_id, body))


@router.post("/scenes", status_code=201)
async def create_scene_asset(body: SceneAssetCreate, service: AnimeServiceDep):
    return _scene_view(await service.create_scene_asset(body))


@router.post("/props", status_code=201)
async def create_prop(body: PropCreate, service: AnimeServiceDep):
    prop = await service.create_prop(body)
    return {
        "id": prop.id,
        "project_id": prop.project_id,
        "name": prop.name,
        "description": prop.description,
        "reference_images": prop.reference_images_json,
        "created_at": prop.created_at,
    }


# --- C2 series structure -----------------------------------------------------------


@router.post("/projects", status_code=201)
async def create_anime_project(body: AnimeProjectCreate, service: AnimeServiceDep):
    project = await service.create_anime_project(body)
    return {
        "id": project.id,
        "project_id": project.project_id,
        "world_setting": project.world_setting,
        "style_profile": project.style_profile,
        "created_at": project.created_at,
    }


@router.post("/episodes", status_code=201)
async def create_episode(body: EpisodeCreate, service: AnimeServiceDep):
    episode = await service.create_episode(body)
    return {
        "id": episode.id,
        "anime_project_id": episode.anime_project_id,
        "season_no": episode.season_no,
        "episode_no": episode.episode_no,
        "title": episode.title,
        "script_summary": episode.script_summary,
        "status": episode.status,
    }


@router.post("/episodes/{episode_id}/scenes", status_code=201)
async def create_scene_in_episode(
    episode_id: str, body: SceneInEpisodeCreate, service: AnimeServiceDep
):
    scene = await service.create_scene_in_episode(body)
    return {
        "id": scene.id,
        "episode_id": scene.episode_id,
        "scene_no": scene.scene_no,
        "location_id": scene.location_id,
        "description": scene.description,
        "characters": scene.characters_json,
        "status": scene.status,
    }


@router.post("/scenes/{scene_id}/shots", status_code=201)
async def create_shot(scene_id: str, body: ShotCreate, service: AnimeServiceDep):
    shot = await service.create_shot(body)
    return {
        "id": shot.id,
        "scene_id": shot.scene_id,
        "shot_no": shot.shot_no,
        "duration_sec": shot.duration_sec,
        "visual_description": shot.visual_description,
        "camera_setup": shot.camera_setup_json,
        "character_states": shot.character_states_json,
        "parent_shot_id": shot.parent_shot_id,
        "status": shot.status,
    }


# --- C3 shot production -------------------------------------------------------------


@router.post("/scenes/{scene_id}/plan", response_model=ShotPlanResponse)
async def plan_shots(scene_id: str, service: AnimeServiceDep):
    return await service.plan_shots(scene_id)


@router.post("/scenes/{scene_id}/generate", response_model=SceneGenerateResponse)
async def generate_scene(scene_id: str, service: AnimeServiceDep):
    return await service.generate_scene(scene_id)


@router.post("/shots/{shot_id}/retry", response_model=ShotGenerateResponse)
async def retry_shot(shot_id: str, service: AnimeServiceDep):
    return await service.retry_shot(shot_id)


@router.get("/scenes/{scene_id}/progress", response_model=ShotProgressResponse)
async def shot_progress(scene_id: str, service: AnimeServiceDep):
    return await service.shot_progress(scene_id)


# --- C4 consistency ------------------------------------------------------------------


@router.get(
    "/characters/{character_id}/consistency-report",
    response_model=ConsistencyReportResponse,
)
async def character_consistency(character_id: str, service: AnimeServiceDep):
    return await service.character_report(character_id)


@router.get("/characters/{character_id}/cross-episode-consistency/{season_no}")
async def cross_episode_consistency(character_id: str, season_no: int, service: AnimeServiceDep):
    return await service.cross_episode_consistency(character_id, season_no)


@router.get("/episodes/{episode_id}/consistency-report")
async def episode_consistency(episode_id: str, service: AnimeServiceDep):
    return await service.episode_report(episode_id)
