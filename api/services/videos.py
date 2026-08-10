"""Application service for the phase 06 short-video pipeline."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.compose import Composer
from pixelle_video.videos.packager import VideoPackager
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine
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
    ):
        self.repository = repository
        self.script_engine = script_engine
        self.storyboard_engine = storyboard_engine
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

    async def build_storyboard(self, script_id: str) -> dict[str, Any]:
        engine = self._storyboard()
        return await engine.build_storyboard(script_id)

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
        return await packager.package(script_id, platforms)

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
