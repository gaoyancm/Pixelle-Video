"""Phase 05 A2: ad production engine.

Reuses the phase 02 media-job kernel, phase 03 production batches, and the
phase 04-A prompt compiler to break a product brief into concrete image,
video, and caption production tasks. No pipeline logic is modified.
"""

from __future__ import annotations

from typing import Any, Callable

from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.products.models import ProductBrief
from pixelle_video.products.repository import ProductBriefRepository

IMAGE_WORKFLOW = "image_default"
VIDEO_WORKFLOW = "a800_wan22_t2v_33f"
CAPTION_EXECUTOR = "llm_caption"


class AdProductionEngine:
    """Plan and start ad production from a product brief."""

    def __init__(
        self,
        brief_repository: ProductBriefRepository,
        batch_repository: ManagementRepository,
        job_repository: MediaJobRepository,
        *,
        prompt_compiler: Callable[..., str] | None = None,
    ):
        self.brief_repository = brief_repository
        self.batch_repository = batch_repository
        self.job_repository = job_repository
        self.prompt_compiler = prompt_compiler

    # --- planning (pure) -------------------------------------------------------

    def plan_production(self, brief: ProductBrief) -> list[dict[str, Any]]:
        """Decompose a brief into concrete production tasks.

        Returns a list of task descriptors; each maps to one MediaJob:
        - 1 main product image (white-background hero)
        - 2 scene images (usage scenes)
        - 1 ad video per platform
        - 1 caption/ad-copy task per platform
        """
        platforms = [p for p in (brief.platforms_json or []) if p]
        if not platforms:
            platforms = ["etsy", "tiktok"]
        tasks: list[dict[str, Any]] = []

        tasks.append(
            {
                "kind": "image",
                "role": "main_image",
                "workflow_type": IMAGE_WORKFLOW,
                "workflow_key": "workflow.json",
                "executor_kind": "comfyui",
                "prompt_hint": (
                    f"白色背景商品主图：{brief.product_name}；卖点："
                    f"{'、'.join(brief.selling_points_json or [])}"
                ),
            }
        )
        for index in range(2):
            tasks.append(
                {
                    "kind": "image",
                    "role": f"scene_image_{index + 1}",
                    "workflow_type": IMAGE_WORKFLOW,
                    "workflow_key": "workflow.json",
                    "executor_kind": "comfyui",
                    "prompt_hint": (
                        f"使用场景图 {index + 1}：{brief.product_name} 在"
                        f"{brief.category or '典型'}场景中的使用"
                    ),
                }
            )
        for platform in platforms:
            tasks.append(
                {
                    "kind": "video",
                    "role": f"ad_video_{platform}",
                    "workflow_type": VIDEO_WORKFLOW,
                    "workflow_key": "workflow.json",
                    "executor_kind": "private_comfyui",
                    "platform": platform,
                    "prompt_hint": (
                        f"{platform} 广告短视频：{brief.product_name}，"
                        f"受众 {brief.target_audience or '广泛'}，时长 15-30 秒"
                    ),
                }
            )
            tasks.append(
                {
                    "kind": "caption",
                    "role": f"caption_{platform}",
                    "workflow_type": "llm_caption",
                    "workflow_key": "caption.json",
                    "executor_kind": CAPTION_EXECUTOR,
                    "platform": platform,
                    "prompt_hint": (
                        f"{platform} 广告文案：产品 {brief.product_name}，"
                        f"卖点 {'、'.join(brief.selling_points_json or [])}，"
                        f"受众 {brief.target_audience or '广泛'}"
                    ),
                }
            )
        return tasks

    # --- production start -------------------------------------------------------

    async def start_production(
        self,
        brief_id: str,
        *,
        executor_kind_override: str | None = None,
    ) -> dict[str, Any]:
        """Create a production batch and one media job per planned task.

        The brief moves to ``processing``; the returned payload lists the
        created job ids grouped by kind for progress tracking.
        """
        brief = await self.brief_repository.get_brief(brief_id)
        if brief is None:
            from pixelle_video.products.repository import ProductBriefNotFoundError

            raise ProductBriefNotFoundError("product brief not found")

        project_id = brief.project_id or "default-project"
        batch = await self.batch_repository.create_batch(
            project_id=project_id,
            name=f"广告生产-{brief.product_name}",
            workflow_type="ad_production",
            common_parameters={
                "brief_id": brief_id,
                "product_name": brief.product_name,
            },
            default_priority=1,
        )

        tasks = self.plan_production(brief)
        created: dict[str, list[str]] = {"image": [], "video": [], "caption": []}
        for task in tasks:
            executor_kind = executor_kind_override or task["executor_kind"]
            job = await self.job_repository.create_job(
                MediaJobCreate(
                    workflow_type=task["workflow_type"],
                    workflow_key=task["workflow_key"],
                    executor_kind=executor_kind,
                    provider=executor_kind,
                    node_id=None,
                    input_json={
                        "role": task["role"],
                        "brief_id": brief_id,
                        "product_name": brief.product_name,
                        "prompt_hint": task["prompt_hint"],
                    },
                    input_assets_json=[],
                    idempotency_key=f"{brief_id}:{task['role']}",
                )
            )
            created[task["kind"]].append(job.job.job_id)

        await self.brief_repository.update_status(brief_id, "processing")
        return {
            "brief_id": brief_id,
            "batch_id": batch.id,
            "tasks": len(tasks),
            "jobs": created,
        }
