"""Phase 06 S2: storyboard and asset generation."""

from __future__ import annotations

from typing import Any, Callable

from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.repository import VideoScriptRepository

STOCK_KEYWORDS_TEMPLATE = "{{topic}} {{visual}}"  # placeholder for optional stock search


class StoryboardEngine:
    """Split a confirmed script into storyboard frames and launch asset jobs."""

    def __init__(
        self,
        script_repository: VideoScriptRepository,
        job_repository: MediaJobRepository,
        *,
        image_workflow: str = "image_default",
        video_workflow: str = "a800_wan22_t2v_33f",
        keyword_builder: Callable[[dict[str, Any]], str] | None = None,
    ):
        self.script_repository = script_repository
        self.job_repository = job_repository
        self.image_workflow = image_workflow
        self.video_workflow = video_workflow
        self.keyword_builder = keyword_builder or (
            lambda scene: f"{scene.get('visual_direction', '')} {scene.get('text', '')}"[:80]
        )

    async def build_storyboard(self, script_id: str) -> dict[str, Any]:
        """Expand the script scenes into storyboard frames with prompts."""
        script = await self._require(script_id)
        scenes = (script.script_json or {}).get("scenes", [])
        frames = []
        for index, scene in enumerate(scenes, start=1):
            visual = scene.get("visual_direction", "")
            frames.append(
                {
                    "index": index,
                    "text": scene.get("text", ""),
                    "duration": scene.get("duration", 5),
                    "visual_description": visual,
                    "image_prompt": self._image_prompt(script, visual),
                    "video_prompt": self._video_prompt(script, visual),
                    "asset_source": "ai_generated",
                    "stock_keywords": self.keyword_builder(scene),
                    "generated_asset_id": None,
                    "status": "pending",
                }
            )
        script_json = dict(script.script_json or {})
        script_json["storyboard"] = {"frames": frames}
        await self.script_repository.update_script(
            script_id, script_json=script_json, status="storyboarding"
        )
        return {"script_id": script_id, "frames": frames}

    async def get_storyboard(self, script_id: str) -> dict[str, Any]:
        script = await self._require(script_id)
        return {
            "script_id": script_id,
            "frames": (script.script_json or {}).get("storyboard", {}).get("frames", []),
        }

    async def generate_assets(
        self, script_id: str, *, executor_kind_override: str | None = None
    ) -> dict[str, Any]:
        """Create one media job per storyboard frame and track progress."""
        script = await self._require(script_id)
        frames = (script.script_json or {}).get("storyboard", {}).get("frames", [])
        job_ids: dict[str, str] = {}
        for frame in frames:
            index = frame["index"]
            is_image = index % 3 != 0  # every third frame is a motion/video frame
            workflow = self.video_workflow if not is_image else self.image_workflow
            executor_kind = executor_kind_override or (
                "private_comfyui" if not is_image else "comfyui"
            )
            job = await self.job_repository.create_job(
                MediaJobCreate(
                    workflow_type=workflow,
                    workflow_key="workflow.json",
                    executor_kind=executor_kind,
                    provider=executor_kind,
                    node_id=None,
                    input_json={
                        "script_id": script_id,
                        "frame_index": index,
                        "prompt": frame.get("image_prompt") or frame.get("video_prompt", ""),
                        "role": f"storyboard_frame_{index}",
                    },
                    input_assets_json=[],
                    idempotency_key=f"{script_id}:frame:{index}",
                )
            )
            job_ids[str(index)] = job.job.job_id
            frame["generated_asset_id"] = job.job.job_id
            frame["status"] = "queued"
        script_json = dict(script.script_json or {})
        script_json["storyboard"]["frames"] = frames
        await self.script_repository.update_script(
            script_id, script_json=script_json, status="assets"
        )
        return {"script_id": script_id, "jobs": job_ids}

    async def progress(self, script_id: str) -> dict[str, Any]:
        script = await self._require(script_id)
        frames = (script.script_json or {}).get("storyboard", {}).get("frames", [])
        states = {"pending": 0, "queued": 0, "completed": 0, "failed": 0}
        for frame in frames:
            job_id = frame.get("generated_asset_id")
            if not job_id:
                states["pending"] += 1
                continue
            job = await self.job_repository.get_job(job_id)
            state = getattr(job, "status", None) if job else None
            if state == "succeeded":
                states["completed"] += 1
                frame["status"] = "completed"
            elif state in {"failed", "canceled"}:
                states["failed"] += 1
                frame["status"] = "failed"
            else:
                states["queued"] += 1
        return {"script_id": script_id, "frames": frames, "states": states}

    async def _require(self, script_id: str):
        script = await self.script_repository.get_script(script_id)
        if script is None:
            from pixelle_video.videos.repository import VideoScriptNotFoundError

            raise VideoScriptNotFoundError("video script not found")
        return script

    @staticmethod
    def _image_prompt(script, visual: str) -> str:
        return f"{script.topic}, {visual}, clean composition, 16:9".strip(", ")

    @staticmethod
    def _video_prompt(script, visual: str) -> str:
        return f"camera moves through: {script.topic}, {visual}, cinematic".strip(", ")
