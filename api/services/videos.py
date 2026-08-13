"""Application service for the phase 06 short-video pipeline."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.compose import Composer
from pixelle_video.videos.packager import VideoPackager
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine
from pixelle_video.videos.script_mapper import ScriptMapper
from pixelle_video.videos.storyboard import StoryboardEngine


class VideoApplicationService:
    """Own short-video business rules and pipeline orchestration."""

    def __init__(
        self,
        repository: VideoScriptRepository,
        *,
        script_engine: ScriptEngine | None = None,
        storyboard_engine: StoryboardEngine | None = None,
        composer: Composer | None = None,
        packager: VideoPackager | None = None,
        job_repository: MediaJobRepository | None = None,
        prompt_compiler: Callable[..., str] | None = None,
        tts_runner: Callable[[str], Awaitable[str]] | None = None,
        bgm_matcher: Callable[[str], Awaitable[str]] | None = None,
        storyboard_planner: Any | None = None,
        plan_repository: Any | None = None,
        script_mapper: Any | None = None,
    ):
        self.repository = repository
        self.script_engine = script_engine
        self.storyboard_engine = storyboard_engine
        self.storyboard_planner = storyboard_planner
        self.plan_repository = plan_repository
        self.script_mapper = script_mapper
        self.composer = composer
        self.packager = packager
        self.job_repository = job_repository
        self.prompt_compiler = prompt_compiler
        self.tts_runner = tts_runner
        self.bgm_matcher = bgm_matcher

    # --- S1 ---------------------------------------------------------------------

    async def generate_script(self, body) -> dict[str, Any]:
        engine = self.script_engine or ScriptEngine(
            self.repository, prompt_compiler=self.prompt_compiler
        )
        return await engine.generate_script(
            topic=body.topic,
            language=body.language,
            target_duration=body.target_duration,
            platform=body.platform,
            project_id=body.project_id,
            reference_image_id=getattr(body, "reference_image_id", None),
        )

    async def get_script(self, script_id: str):
        return await self.repository.get_script(script_id)

    async def update_script(self, script_id: str, body) -> Any:
        return await self.repository.update_script(
            script_id, script_json=body.script_json, status=body.status
        )

    async def confirm(self, script_id: str) -> dict[str, Any]:
        script = await self.repository.update_status(script_id, "confirmed")
        return {"script_id": script_id, "status": script.status}

    # --- S2 ---------------------------------------------------------------------

    async def create_script_from_plan(
        self, plan_id: str, reference_image_id: str | None = None
    ) -> dict[str, Any]:
        """B1: auto-map a 04-E Content Plan onto a video script."""
        if self.plan_repository is None:
            raise RuntimeError("plan repository not configured")
        plan = await self.plan_repository.get_plan(plan_id)
        if plan is None:
            from pixelle_video.orchestration.repository import ContentPlanNotFoundError

            raise ContentPlanNotFoundError("content plan not found")
        if plan.intent != "short_video":
            raise ValueError(f"plan intent {plan.intent} is not short_video")
        mapper = self.script_mapper or ScriptMapper()
        mapped = mapper.map(plan)
        script = await self.repository.create_script(
            topic=mapped["topic"],
            script_json=mapped["script_json"],
            platform=mapped["platform"],
            target_duration=mapped["target_duration"],
            language=mapped["language"],
            project_id=plan.project_id,
            reference_image_id=reference_image_id,
        )
        return {
            "script_id": script.id,
            "plan_id": plan.id,
            "topic": script.topic,
            "platform": script.platform,
            "target_duration": script.target_duration,
            "reference_image_id": script.reference_image_id,
        }

    async def get_plan_for_script(self, script_id: str) -> dict[str, Any] | None:
        """B1: trace back from a video script to its source content plan."""
        script = await self.repository.get_script(script_id)
        if script is None or self.plan_repository is None:
            return None
        plan_id = ScriptMapper.plan_id_from_script(script)
        if plan_id is None:
            return None
        plan = await self.plan_repository.get_plan(plan_id)
        if plan is None:
            return None
        return {
            "plan_id": plan.id,
            "intent": plan.intent,
            "status": plan.status,
            "summary": (plan.plan_json or {}).get("summary", ""),
            "cost_estimate": plan.cost_estimate,
        }

    async def confirm_from_plan(self, script_id: str) -> dict[str, Any]:
        """B3: one-step confirm -> storyboard -> composing."""
        script = await self.repository.get_script(script_id)
        if script is None:
            from pixelle_video.videos.repository import VideoScriptNotFoundError

            raise VideoScriptNotFoundError("video script not found")
        board = await self.build_storyboard(script_id)
        await self.repository.update_script(script_id, status="composing")
        return {
            "script_id": script_id,
            "frames": board.get("frames", []),
            "source": board.get("source", "engine"),
        }

    async def build_storyboard(self, script_id: str) -> dict[str, Any]:
        if self.storyboard_planner is not None:
            return await self._build_storyboard_via_agent(script_id)
        engine = self._storyboard()
        return await engine.build_storyboard(script_id)

    async def _build_storyboard_via_agent(self, script_id: str) -> dict[str, Any]:
        """B2: route storyboard generation through the 04-E storyboard planner."""
        script = await self.repository.get_script(script_id)
        if script is None:
            from pixelle_video.videos.repository import VideoScriptNotFoundError

            raise VideoScriptNotFoundError("video script not found")
        script_json = script.script_json or {}
        visual_style = script_json.get("visual_style") or {}
        style_hint = str(visual_style.get("mood") or visual_style.get("style") or "自然")
        prompt = (
            f"[run_storyboard_planner] 为短视频脚本「{script.topic}」设计分镜。"
            f"时长 {script.target_duration} 秒，平台 {script.platform}，"
            f"视觉风格：{style_hint}。输出 scenes（含 index/desc/camera）与 camera_notes。"
        )
        result = await self.storyboard_planner.run(prompt)
        content = result.content
        updated_json = dict(script_json)
        updated_json["storyboard"] = {
            "scenes": content.get("scenes", []),
            "camera_notes": content.get("camera_notes", ""),
        }
        await self.repository.update_script(
            script_id, script_json=updated_json, status="storyboarding"
        )
        return {
            "script_id": script_id,
            "frames": content.get("scenes", []),
            "camera_notes": content.get("camera_notes", ""),
            "source": "04-e-storyboard-planner",
        }

    async def get_storyboard(self, script_id: str) -> dict[str, Any]:
        engine = self._storyboard()
        return await engine.get_storyboard(script_id)

    async def generate_assets(
        self, script_id: str, *, executor_kind_override: str | None = None
    ) -> dict[str, Any]:
        engine = self._storyboard()
        return await engine.generate_assets(
            script_id, executor_kind_override=executor_kind_override
        )

    async def asset_progress(self, script_id: str) -> dict[str, Any]:
        engine = self._storyboard()
        return await engine.progress(script_id)

    # --- S3 ---------------------------------------------------------------------

    async def compose(self, script_id: str) -> dict[str, Any]:
        composer = self.composer or Composer(
            self.repository,
            tts_runner=self.tts_runner,
            bgm_matcher=self.bgm_matcher,
        )
        return await composer.compose(script_id)

    async def compose_status(self, script_id: str) -> dict[str, Any]:
        composer = self.composer or Composer(self.repository)
        return await composer.compose_status(script_id)

    # --- S4 ---------------------------------------------------------------------

    async def package(self, script_id: str, platforms: list[str]) -> dict[str, Any]:
        packager = self.packager or VideoPackager(self.repository)
        video_path = self.result_path(script_id)
        return await packager.package(script_id, platforms, video_path=video_path)

    def download_zip(self, script_id: str, project_id: str | None) -> str | None:
        packager = self.packager or VideoPackager(self.repository)
        return packager.zip_package(script_id, project_id)

    def result_path(self, script_id: str) -> str | None:
        composer = self.composer or Composer(self.repository)
        return composer.result_path(script_id)

    # --- helpers -----------------------------------------------------------------

    def _storyboard(self) -> StoryboardEngine:
        if self.storyboard_engine is not None:
            return self.storyboard_engine
        if self.job_repository is None:
            raise RuntimeError("job repository not configured for storyboard")
        return StoryboardEngine(self.repository, self.job_repository)

    async def _require(self, script_id: str):
        script = await self.repository.get_script(script_id)
        if script is None:
            from pixelle_video.videos.repository import VideoScriptNotFoundError

            raise VideoScriptNotFoundError("video script not found")
        return script
