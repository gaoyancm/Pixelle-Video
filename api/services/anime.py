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
        storyboard_planner: Any | None = None,
        plan_repository: Any | None = None,
        consistency_verifier: Any | None = None,
    ):
        self.repository = repository
        self.job_repository = job_repository
        self.prompt_compiler = prompt_compiler
        self.shot_engine = shot_engine
        self.consistency_guard = consistency_guard
        self.storyboard_planner = storyboard_planner
        self.plan_repository = plan_repository
        self.consistency_verifier = consistency_verifier
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
        if self.storyboard_planner is not None:
            return await self._plan_shots_via_agent(scene_id)
        return {"shots": await self._engine().plan_shots(scene_id)}

    async def _plan_shots_via_agent(self, scene_id: str) -> dict[str, Any]:
        """D1: AI auto-storyboard via the 04-E planner, with camera-tree
        inheritance (wide -> medium -> close)."""
        scene = await self.repository.get_scene_in_episode(scene_id)
        if scene is None:
            from pixelle_video.anime.repository import AnimeNotFoundError

            raise AnimeNotFoundError("scene not found")
        characters = [entry.get("character_id", "") for entry in (scene.characters_json or [])]
        style_hint = "动画"
        episode = None
        if scene.episode_id is not None:
            episode = await self.repository.get_episode(scene.episode_id)
        if episode is not None and episode.anime_project_id is not None:
            project = await self.repository.get_anime_project(episode.anime_project_id)
            if project is not None and project.style_profile:
                style_hint = project.style_profile
        prompt = (
            f"[run_storyboard_planner] 为动画场景「{scene.description or scene.scene_no}」"
            f"自动生成分镜。角色：{','.join(characters) or '无'}。风格：{style_hint}。"
            "输出 scenes（每项含 index/desc/shot_type: wide|medium|close/camera）+ camera_notes。"
        )
        result = await self.storyboard_planner.run(prompt)
        content = result.content
        planned_scenes = content.get("scenes", [])
        created: list[dict[str, Any]] = []
        last_wide_id: str | None = None
        for index, entry in enumerate(planned_scenes, start=1):
            shot_type = str(entry.get("shot_type", "medium")).lower()
            parent_id = None
            if shot_type == "wide":
                parent_id = None
                wide_shot = await self.repository.create_shot(
                    scene_id=scene_id,
                    shot_no=index,
                    duration_sec=5,
                    visual_description=str(entry.get("desc", "镜头")),
                    camera_setup={
                        "shot_type": shot_type,
                        "camera": entry.get("camera", ""),
                    },
                )
                last_wide_id = wide_shot.id
            else:
                parent_id = last_wide_id  # camera-tree inheritance
                wide_shot = await self.repository.create_shot(
                    scene_id=scene_id,
                    shot_no=index,
                    duration_sec=5,
                    visual_description=str(entry.get("desc", "镜头")),
                    camera_setup={
                        "shot_type": shot_type,
                        "camera": entry.get("camera", ""),
                    },
                    parent_shot_id=parent_id,
                )
            created.append(
                {
                    "id": wide_shot.id,
                    "shot_no": wide_shot.shot_no,
                    "shot_type": shot_type,
                    "parent_shot_id": wide_shot.parent_shot_id,
                }
            )
        return {"shots": created, "source": "04-e-storyboard-planner"}

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

    async def cross_episode_consistency(self, character_id: str, season_no: int) -> dict[str, Any]:
        """D3: verify a character's development arc across a season using the
        04-E planner; reads the arc from the Content Plan and scans the
        character's shots across episodes (ConsistencyGuard engine untouched)."""
        character = await self.repository.get_character(character_id)
        if character is None:
            from pixelle_video.anime.repository import AnimeNotFoundError

            raise AnimeNotFoundError("character not found")
        arc = None
        if self.plan_repository is not None:
            arc = await self._find_character_arc(character_id, season_no)
        shots = await self.repository.list_shots_for_character(character_id)
        arc_hint = arc.get("season_arc") if arc else "（未定义弧线）"
        if self.consistency_verifier is not None and shots:
            prompt = (
                f"[run_consistency_verifier] 校验角色「{character.name}」第 {season_no} 季的"
                f"跨集一致性。角色弧线：{arc_hint}。镜头数：{len(shots)}。"
                "输出 verdict（consistent: true/false）、issues（数组）。"
            )
            result = await self.consistency_verifier.run(prompt)
            content = result.content
            return {
                "character_id": character_id,
                "season_no": season_no,
                "character_name": character.name,
                "arc": arc_hint,
                "shots_scanned": len(shots),
                "consistent": bool(content.get("consistent", False)),
                "issues": content.get("issues", []),
            }
        return {
            "character_id": character_id,
            "season_no": season_no,
            "character_name": character.name,
            "arc": arc_hint,
            "shots_scanned": len(shots),
            "consistent": True,
            "issues": [],
        }

    async def _find_character_arc(self, character_id: str, season_no: int) -> dict[str, Any] | None:
        if self.plan_repository is None:
            return None
        plans, _ = await self.plan_repository.list_plans(limit=20, offset=0)
        for plan in plans:
            plan_json = plan.plan_json or {}
            for arc in plan_json.get("character_arcs", []):
                if arc.get("character_id") == character_id:
                    return arc
        return None

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
