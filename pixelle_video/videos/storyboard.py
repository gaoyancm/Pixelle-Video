"""Phase 06 S2: storyboard and asset generation."""

from __future__ import annotations

from typing import Any, Callable

from pixelle_video.media_jobs.contracts import MediaInputAsset, MediaJobCreate
from pixelle_video.media_jobs.database import MediaJobsDisabledError
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.repository import VideoScriptRepository

STOCK_KEYWORDS_TEMPLATE = "{{topic}} {{visual}}"  # placeholder for optional stock search
I2V_WORKFLOW = "gpu_4090_wan21_i2v_33f"  # requires_image=True (first-frame I2V)


class StoryboardEngine:
    """Split a confirmed script into storyboard frames and launch asset jobs."""

    def __init__(
        self,
        script_repository: VideoScriptRepository,
        job_repository: MediaJobRepository,
        *,
        image_workflow: str = "image_qwen",
        video_workflow: str = "a800_wan22_t2v_33f",
        keyword_builder: Callable[[dict[str, Any]], str] | None = None,
        node_selector: Callable[[str], str] | None = None,
    ):
        self.script_repository = script_repository
        self.job_repository = job_repository
        self.image_workflow = image_workflow
        self.video_workflow = video_workflow
        self.keyword_builder = keyword_builder or (
            lambda scene: f"{scene.get('visual_direction', '')} {scene.get('text', '')}"[:80]
        )
        self.node_selector = node_selector

    async def build_storyboard(
        self,
        script_id: str,
        *,
        scenes: list[dict[str, Any]] | None = None,
        camera_notes: str | None = None,
    ) -> dict[str, Any]:
        """Expand the script scenes into storyboard frames with prompts."""
        script = await self._require(script_id)
        source_scenes = scenes if scenes is not None else (script.script_json or {}).get("scenes", [])
        frames = []
        for index, scene in enumerate(source_scenes, start=1):
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
                    "camera": scene.get("camera"),
                    "job_id": None,
                    "asset_id": None,
                    "status": "pending",
                }
            )
        script_json = dict(script.script_json or {})
        script_json["storyboard"] = {"frames": frames, "camera_notes": camera_notes or ""}
        await self.script_repository.update_script(
            script_id, script_json=script_json, status="storyboarding"
        )
        return {"script_id": script_id, "frames": frames, "camera_notes": camera_notes}

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
        reference_image_id = getattr(script, "reference_image_id", None)
        planned: list[tuple[dict[str, Any], str, list[MediaInputAsset], str, str | None]] = []
        for frame in frames:
            index = frame["index"]
            is_image = index % 3 != 0
            input_assets: list[MediaInputAsset] = []
            if reference_image_id:
                candidates = (
                    ("sdxl_img2img", self.image_workflow)
                    if is_image
                    else (I2V_WORKFLOW, self.video_workflow)
                )
                input_assets = [MediaInputAsset(asset_id=reference_image_id, role="input_image")]
            else:
                candidates = (self.image_workflow,) if is_image else (self.video_workflow,)
            executor_kind = executor_kind_override or "private_comfyui"
            node_id = None
            workflow = candidates[0]
            if executor_kind == "private_comfyui":
                if self.node_selector is None:
                    raise MediaJobsDisabledError
                selected = self._select_available(candidates)
                if selected is None:
                    raise MediaJobsDisabledError from None
                workflow, node_id = selected
            planned.append((frame, workflow, input_assets, executor_kind, node_id))

        job_ids: dict[str, str] = {}
        for frame, workflow, input_assets, executor_kind, node_id in planned:
            index = frame["index"]
            create = MediaJobCreate(
                workflow_type=workflow,
                workflow_key="workflow.json",
                executor_kind=executor_kind,
                provider=executor_kind,
                node_id=node_id,
                input_json={
                    "script_id": script_id,
                    "frame_index": index,
                    "prompt": (
                        frame.get("image_prompt", "")
                        if index % 3 != 0
                        else frame.get("video_prompt", "")
                    ),
                    "role": f"storyboard_frame_{index}",
                },
                input_assets_json=input_assets,
                idempotency_key=f"{script_id}:frame:{index}",
            )
            if input_assets:
                job = await self.job_repository.create_job_with_assets(create)
            else:
                job = await self.job_repository.create_job(create)
            job_ids[str(index)] = job.job.job_id
            # `job_id` is the media job; `asset_id` is the final resolved asset
            # and stays None until the job completes and its output is resolved.
            frame["job_id"] = job.job.job_id
            frame["asset_id"] = None
            frame["status"] = "queued"
        script_json = dict(script.script_json or {})
        script_json["storyboard"]["frames"] = frames
        await self.script_repository.update_script(
            script_id, script_json=script_json, status="assets"
        )
        return {"script_id": script_id, "jobs": job_ids}

    def _select_available(self, workflows: tuple[str, ...]) -> tuple[str, str] | None:
        """Select the first workflow with an enabled node, preserving preference order."""

        if self.node_selector is None:
            return None
        for workflow in workflows:
            try:
                return workflow, self.node_selector(workflow)
            except RuntimeError:
                continue
        return None

    async def progress(self, script_id: str) -> dict[str, Any]:
        script = await self._require(script_id)
        frames = (script.script_json or {}).get("storyboard", {}).get("frames", [])
        states = {"pending": 0, "queued": 0, "completed": 0, "failed": 0}
        for frame in frames:
            job_id = frame.get("job_id")
            if not job_id:
                states["pending"] += 1
                continue
            job = await self.job_repository.get_job(job_id)
            state = getattr(job, "status", None) if job else None
            if state == "succeeded":
                states["completed"] += 1
                frame["status"] = "completed"
            elif state in {"failed", "cancelled", "timed_out"}:
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
