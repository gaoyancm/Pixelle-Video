"""Application service for the phase 07 anime pipeline."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.anime.consistency import ConsistencyGuard
from pixelle_video.anime.repository import AnimeRepository
from pixelle_video.anime.shot_engine import ShotProductionEngine
from pixelle_video.media_jobs.repository import MediaJobRepository


class AnimeApplicationService:
    """Own anime asset, series, shot, and consistency business rules."""

    def __init__(
        self,
        repository: AnimeRepository,
        *,
        job_repository: MediaJobRepository | None = None,
        prompt_compiler: Callable[..., str] | None = None,
        shot_engine: ShotProductionEngine | None = None,
        consistency_guard: ConsistencyGuard | None = None,
        qc_runner: Callable[[str], Awaitable[Any]] | None = None,
    ):
        self.repository = repository
        self.job_repository = job_repository
        self.prompt_compiler = prompt_compiler
        self.shot_engine = shot_engine
        self.consistency_guard = consistency_guard
        self.qc_runner = qc_runner

    # --- C1 ---------------------------------------------------------------------

    async def create_character(self, body) -> Any:
        anchors = body.identity_anchors
        if body.auto_fill_anchors and not anchors:
            anchors = auto_fill_anchors(body.description)
        return await self.repository.create_character(
            name=body.name,
            description=body.description,
            project_id=body.project_id,
            role_type=body.role_type,
            identity_anchors=anchors,
            static_features=body.static_features,
            dynamic_features=body.dynamic_features,
            reference_images=body.reference_images,
            voice_config=body.voice_config,
        )

    async def list_characters(self, project_id: str | None, limit: int, offset: int):
        return await self.repository.list_characters(project_id, limit + 1, offset)

    async def update_character(self, character_id: str, body) -> Any:
        return await self.repository.update_character(
            character_id,
            name=body.name,
            description=body.description,
            identity_anchors=body.identity_anchors,
            static_features=body.static_features,
            dynamic_features=body.dynamic_features,
            reference_images=body.reference_images,
        )

    async def create_scene_asset(self, body) -> Any:
        return await self.repository.create_scene_asset(
            name=body.name,
            description=body.description,
            project_id=body.project_id,
            environment=body.environment,
            reference_images=body.reference_images,
        )

    async def create_prop(self, body) -> Any:
        return await self.repository.create_prop(
            name=body.name,
            project_id=body.project_id,
            description=body.description,
            reference_images=body.reference_images,
        )

    # --- C2 ---------------------------------------------------------------------

    async def create_anime_project(self, body) -> Any:
        return await self.repository.create_anime_project(
            project_id=body.project_id,
            world_setting=body.world_setting,
            style_profile=body.style_profile,
        )

    async def create_episode(self, body) -> Any:
        return await self.repository.create_episode(
            anime_project_id=body.anime_project_id,
            season_no=body.season_no,
            episode_no=body.episode_no,
            title=body.title,
            script_summary=body.script_summary,
        )

    async def create_scene_in_episode(self, body) -> Any:
        return await self.repository.create_scene_in_episode(
            episode_id=body.episode_id,
            scene_no=body.scene_no,
            description=body.description,
            location_id=body.location_id,
            characters=body.characters,
        )

    async def create_shot(self, body) -> Any:
        return await self.repository.create_shot(
            scene_id=body.scene_id,
            shot_no=body.shot_no,
            duration_sec=body.duration_sec,
            visual_description=body.visual_description,
            camera_setup=body.camera_setup,
            character_states=body.character_states,
            parent_shot_id=body.parent_shot_id,
            reference_shot_ids=body.reference_shot_ids,
        )

    # --- C3 ---------------------------------------------------------------------

    def _engine(self) -> ShotProductionEngine:
        if self.shot_engine is not None:
            return self.shot_engine
        if self.job_repository is None:
            raise RuntimeError("job repository not configured for shots")
        return ShotProductionEngine(
            self.repository, self.job_repository, prompt_compiler=self.prompt_compiler
        )

    async def plan_shots(self, scene_id: str) -> dict[str, Any]:
        return {"shots": await self._engine().plan_shots(scene_id)}

    async def generate_scene(self, scene_id: str) -> dict[str, Any]:
        return await self._engine().generate_scene(scene_id)

    async def retry_shot(self, shot_id: str) -> dict[str, Any]:
        return await self._engine().retry_shot(shot_id)

    async def shot_progress(self, scene_id: str) -> dict[str, Any]:
        return await self._engine().progress(scene_id)

    # --- C4 ---------------------------------------------------------------------

    def _guard(self) -> ConsistencyGuard:
        if self.consistency_guard is not None:
            return self.consistency_guard
        return ConsistencyGuard(self.repository)

    async def character_report(self, character_id: str) -> dict[str, Any]:
        return await self._guard().character_report(character_id)

    async def episode_report(self, episode_id: str) -> dict[str, Any]:
        return await self._guard().episode_report(episode_id)


def auto_fill_anchors(description: str) -> dict[str, Any]:
    """Deterministic 6-layer anchor auto-fill from a character description."""
    return {
        "bone_structure": {"face_shape": "oval", "jawline": "defined", "cheekbones": "high"},
        "facial_features": {"eye_shape": "almond", "nose_shape": "straight", "lip_shape": "full"},
        "unique_marks": [],
        "color_palette": {
            "iris": "#3D2314",
            "hair": "#1A1A1A",
            "skin": "#E8C4A0",
            "lips": "#C4727E",
        },
        "texture": {"skin_texture": "smooth"},
        "hair": {"hair_style": "束发高髻", "hairline": "美人尖"},
        "source_hint": description[:80],
    }
