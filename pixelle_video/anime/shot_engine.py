"""Phase 07 C3: shot planning and production engine."""

from __future__ import annotations

from typing import Any, Callable

from pixelle_video.anime.repository import AnimeRepository
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.repository import MediaJobRepository

VIDEO_WORKFLOW = "a800_wan22_t2v_33f"


class ShotProductionEngine:
    """Plan a scene into shots, generate each shot, and support local redo."""

    def __init__(
        self,
        repository: AnimeRepository,
        job_repository: MediaJobRepository,
        *,
        prompt_compiler: Callable[..., str] | None = None,
        image_prompt_builder: Callable[[dict[str, Any], str], str] | None = None,
        video_prompt_builder: Callable[[dict[str, Any], str], str] | None = None,
    ):
        self.repository = repository
        self.job_repository = job_repository
        self.prompt_compiler = prompt_compiler
        self.image_prompt_builder = image_prompt_builder or _default_image_prompt
        self.video_prompt_builder = video_prompt_builder or _default_video_prompt

    async def plan_shots(self, scene_id: str) -> list[dict[str, Any]]:
        """Compile per-shot image/video prompts from the shot + scene content."""
        await self._require_scene(scene_id)
        shots = await self.repository.list_shots(scene_id)
        compiled: list[dict[str, Any]] = []
        for shot in shots:
            character_block = "；".join(
                f"{state.get('char_id', '')}({state.get('emotion', '')})"
                for state in (shot.character_states_json or [])
            )
            context = {
                "shot": shot.visual_description,
                "characters": character_block or "无",
                "camera": str(shot.camera_setup_json or {}),
                "style": "动画",
            }
            image_prompt = self.image_prompt_builder(shot, context)
            video_prompt = self.video_prompt_builder(shot, context)
            if self.prompt_compiler is not None:
                image_prompt = self.prompt_compiler(image_prompt, context)
                video_prompt = self.prompt_compiler(video_prompt, context)
            await self.repository.update_shot(
                shot.id, image_prompt=image_prompt, video_prompt=video_prompt
            )
            compiled.append(
                {
                    "id": shot.id,
                    "shot_no": shot.shot_no,
                    "image_prompt": image_prompt,
                    "video_prompt": video_prompt,
                }
            )
        return compiled

    async def generate_shot(
        self,
        shot_id: str,
        *,
        executor_kind_override: str | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        """Create a media job for one shot (reused for redo)."""
        import uuid as _uuid

        shot = await self._require_shot(shot_id)
        if shot.image_prompt is None:
            await self.plan_shots(shot.scene_id)
            shot = await self.repository.get_shot(shot_id)
        executor_kind = executor_kind_override or "private_comfyui"
        idempotency_key = f"anime-shot:{shot.id}"
        if retry:
            idempotency_key = f"anime-shot:{shot.id}:retry:{_uuid.uuid4().hex[:8]}"
        job = await self.job_repository.create_job(
            MediaJobCreate(
                workflow_type=VIDEO_WORKFLOW,
                workflow_key="workflow.json",
                executor_kind=executor_kind,
                provider=executor_kind,
                node_id=None,
                input_json={
                    "shot_id": shot.id,
                    "scene_id": shot.scene_id,
                    "role": f"shot_{shot.shot_no}",
                    "image_prompt": shot.image_prompt,
                    "video_prompt": shot.video_prompt,
                },
                input_assets_json=[],
                idempotency_key=idempotency_key,
            )
        )
        await self.repository.update_shot(
            shot.id, status="queued", generated_asset_id=job.job.job_id
        )
        return {
            "shot_id": shot.id,
            "status": "queued",
            "job_id": job.job.job_id,
            "parent_shot_id": shot.parent_shot_id,
        }

    async def generate_scene(
        self, scene_id: str, *, executor_kind_override: str | None = None
    ) -> dict[str, Any]:
        """Generate every shot in a scene; parent shots go first."""
        shots = await self.repository.list_shots(scene_id)
        ordered = sorted(shots, key=lambda shot: (shot.parent_shot_id is not None, shot.shot_no))
        results = []
        for shot in ordered:
            results.append(
                await self.generate_shot(shot.id, executor_kind_override=executor_kind_override)
            )
        return {"scene_id": scene_id, "shots": results}

    async def retry_shot(self, shot_id: str) -> dict[str, Any]:
        """Local redo: only rebuild this shot's media job."""
        return await self.generate_shot(shot_id, retry=True)

    async def progress(self, scene_id: str) -> dict[str, Any]:
        shots = await self.repository.list_shots(scene_id)
        states = {"pending": 0, "queued": 0, "succeeded": 0, "failed": 0}
        for shot in shots:
            states[shot.status if shot.status in states else "pending"] += 1
        return {
            "scene_id": scene_id,
            "total": len(shots),
            "states": states,
            "shots": [
                {
                    "id": shot.id,
                    "shot_no": shot.shot_no,
                    "status": shot.status,
                    "generated_asset_id": shot.generated_asset_id,
                }
                for shot in shots
            ],
        }

    async def _require_scene(self, scene_id: str):
        scene = await self.repository.get_scene_in_episode(scene_id)
        if scene is None:
            from pixelle_video.anime.repository import AnimeNotFoundError

            raise AnimeNotFoundError("scene not found")
        return scene

    async def _require_shot(self, shot_id: str):
        shot = await self.repository.get_shot(shot_id)
        if shot is None:
            from pixelle_video.anime.repository import AnimeNotFoundError

            raise AnimeNotFoundError("shot not found")
        return shot


def _default_image_prompt(shot, context: dict[str, Any]) -> str:
    return (
        f"动画镜头：{shot.visual_description}。"
        f"角色：{context['characters']}。镜头：{context['camera']}。"
        f"风格：{context['style']}"
    )


def _default_video_prompt(shot, context: dict[str, Any]) -> str:
    return (
        f"镜头运动：{context['camera']}，画面：{shot.visual_description}，"
        f"角色：{context['characters']}，时长 {shot.duration_sec} 秒"
    )
