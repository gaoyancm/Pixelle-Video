"""Application service for the phase 05 product & ad pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.products.ad_engine import AdProductionEngine
from pixelle_video.products.brief_mapper import BriefMapper
from pixelle_video.products.delivery import DeliveryPackager
from pixelle_video.products.platform_adapter import PlatformAdapter
from pixelle_video.products.repository import ProductBriefRepository

_HOOK_TEMPLATE = (
    "为产品「{{product}}」编写一条短视频/广告开场 Hook。"
    "核心卖点：{{points}}；目标受众：{{audience}}"
)


class ProductDeliveryNotReadyError(RuntimeError):
    """The brief cannot be packaged until every required output is durable."""


class ProductApplicationService:
    """Own product-brief business rules and the ad production orchestration."""

    def __init__(
        self,
        repository: ProductBriefRepository,
        *,
        ad_engine: AdProductionEngine | None = None,
        asset_repository: AssetRepository | None = None,
        job_repository: MediaJobRepository | None = None,
        platform_adapter: PlatformAdapter | None = None,
        delivery_packager: DeliveryPackager | None = None,
        prompt_compiler: Callable[..., str] | None = None,
        qc_runner: Callable[[str], Any] | None = None,
        copywriter_agent: Any | None = None,
        plan_repository: Any | None = None,
        brief_mapper: Any | None = None,
        asset_path_resolver: Callable[[Any], Path] | None = None,
        require_complete_outputs: bool = True,
    ):
        self.repository = repository
        self.copywriter_agent = copywriter_agent
        self.plan_repository = plan_repository
        self.brief_mapper = brief_mapper
        self.asset_path_resolver = asset_path_resolver
        self.require_complete_outputs = require_complete_outputs
        self.ad_engine = ad_engine
        self.asset_repository = asset_repository
        self.job_repository = job_repository
        self.platform_adapter = platform_adapter or PlatformAdapter()
        self.delivery_packager = delivery_packager
        self.prompt_compiler = prompt_compiler
        self.qc_runner = qc_runner

    # --- A1 CRUD -----------------------------------------------------------------

    async def create(self, body) -> Any:
        return await self.repository.create_brief(
            project_id=body.project_id,
            product_name=body.product_name,
            category=body.category,
            description=body.description,
            selling_points=body.selling_points,
            target_audience=body.target_audience,
            brand_profile_id=body.brand_profile_id,
            platforms=body.platforms,
            reference_images=body.reference_images,
            plan_id=getattr(body, "plan_id", None),
        )

    async def list(self, project_id: str | None, status: str | None, limit: int, offset: int):
        return await self.repository.list_briefs(
            project_id=project_id, status=status, limit=limit, offset=offset
        )

    async def get(self, brief_id: str):
        return await self.repository.get_brief(brief_id)

    async def update(self, brief_id: str, body) -> Any:
        return await self.repository.update_brief(
            brief_id,
            product_name=body.product_name,
            description=body.description,
            category=body.category,
            selling_points=body.selling_points,
            target_audience=body.target_audience,
            platforms=body.platforms,
            status=body.status,
        )

    # --- A1 generate ideas (via 04-A prompt compiler) ------------------------------

    async def create_brief_from_plan(self, plan_id: str, reference_images=None) -> dict[str, Any]:
        """A1: auto-map a 04-E Content Plan onto a product brief."""
        if self.plan_repository is None:
            raise RuntimeError("plan repository not configured")
        plan = await self.plan_repository.get_plan(plan_id)
        if plan is None:
            from pixelle_video.orchestration.repository import ContentPlanNotFoundError

            raise ContentPlanNotFoundError("content plan not found")
        mapper = self.brief_mapper or BriefMapper()
        mapped = mapper.map(plan)
        brief = await self.repository.create_brief(
            product_name=mapped["product_name"],
            description=mapped["description"],
            project_id=mapped["project_id"],
            selling_points=mapped["selling_points_json"],
            target_audience=mapped["target_audience"],
            platforms=mapped["platforms_json"],
            reference_images=reference_images
            if reference_images
            else mapped["reference_images_json"],
            plan_id=mapped["plan_id"],
        )
        return {
            "brief_id": brief.id,
            "plan_id": plan.id,
            "product_name": brief.product_name,
            "description": brief.description,
            "target_audience": brief.target_audience,
            "selling_points": brief.selling_points_json,
            "platforms": brief.platforms_json,
            "reference_images": brief.reference_images_json,
        }

    async def get_plan_for_brief(self, brief_id: str) -> dict[str, Any] | None:
        """A1: trace back from a brief to its source content plan."""
        brief = await self._require(brief_id)
        if self.plan_repository is None:
            return None
        plan_id = getattr(brief, "plan_id", None)
        if not plan_id:
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

    async def confirm_from_plan(self, brief_id: str) -> dict[str, Any]:
        """A3: one-step confirm + generate-ideas + start production."""
        await self._require(brief_id)
        ideas_payload = await self.generate_ideas(brief_id)
        if self.ad_engine is None:
            raise RuntimeError("ad engine not configured")
        production = await self.ad_engine.start_production(brief_id)
        await self.repository.update_status(brief_id, "processing")
        return {
            "brief_id": brief_id,
            "status": "processing",
            "ideas": ideas_payload.get("ideas", []),
            "source": ideas_payload.get("source", "template"),
            **production,
        }

    async def generate_ideas(self, brief_id: str) -> dict[str, Any]:
        brief = await self._require(brief_id)
        if self.copywriter_agent is not None:
            return await self._generate_ideas_via_agent(brief)
        return await self._generate_ideas_template(brief)

    async def _generate_ideas_via_agent(self, brief) -> dict[str, Any]:
        """A2: route copy generation through the 04-E copywriter sub-agent."""
        creative_directions = []
        plan_id = getattr(brief, "plan_id", None)
        if plan_id and self.plan_repository is not None:
            plan = await self.plan_repository.get_plan(plan_id)
            if plan is not None:
                creative_directions = (plan.plan_json or {}).get("creative_directions", [])
        direction_hint = ""
        if creative_directions:
            direction_hint = "；".join(
                str(d.get("angle") or d.get("hook") or d) for d in creative_directions[:2]
            )
        prompt = (
            f"[run_copywriter] 为「{brief.product_name}」撰写广告文案。"
            f"卖点：{'、'.join(brief.selling_points_json or [brief.description[:40]])}。"
            f"受众：{brief.target_audience or '广泛受众'}。"
            f"创意方向：{direction_hint or '悬念型、利益型、场景型'}。"
            "输出 hooks（3 条）、ctas（3 条）、body_copy。"
        )
        result = await self.copywriter_agent.run(prompt)
        content = result.content
        hooks = content.get("hooks", []) or [brief.product_name]
        ctas = content.get("ctas", []) or ["立即下单，限量优惠"]
        styles = ["悬念型", "利益型", "场景型"]
        ideas = []
        for index, style in enumerate(styles):
            ideas.append(
                {
                    "hook": hooks[index % len(hooks)],
                    "headline": f"{brief.product_name}｜{'、'.join(brief.selling_points_json or [])}",
                    "cta": ctas[index % len(ctas)],
                    "style": style,
                }
            )
        return {"brief_id": brief.id, "ideas": ideas, "source": "04-e-copywriter"}

    async def _generate_ideas_template(self, brief) -> dict[str, Any]:
        """Legacy template path (kept for backwards compatibility)."""
        points = "、".join(brief.selling_points_json or [brief.description[:40]])
        audience = brief.target_audience or "广泛受众"
        styles = ["悬念型", "利益型", "场景型"]
        ideas: list[dict[str, str]] = []
        for index, style in enumerate(styles, start=1):
            if self.prompt_compiler is not None:
                base = self.prompt_compiler(
                    _HOOK_TEMPLATE,
                    {
                        "product": brief.product_name,
                        "points": points,
                        "audience": audience,
                    },
                )
            else:
                base = _HOOK_TEMPLATE.format(
                    product=brief.product_name, points=points, audience=audience
                )
            ideas.append(
                {
                    "hook": f"{style}开场：{base}",
                    "headline": f"{brief.product_name}｜{'、'.join(brief.selling_points_json or [])}",
                    "cta": f"立即下单{index if index > 1 else ''}，限量优惠",
                    "style": style,
                }
            )
        return {"brief_id": brief.id, "ideas": ideas}

    # --- A1 confirm -> A2 production -------------------------------------------------

    async def confirm(self, brief_id: str) -> dict[str, Any]:
        if self.ad_engine is None:
            raise RuntimeError("ad engine not configured")
        result = await self.ad_engine.start_production(brief_id)
        await self.repository.update_status(brief_id, "processing")
        return {"brief_id": brief_id, "status": "processing", **result}

    # --- A2 progress / results -------------------------------------------------------

    async def progress(self, brief_id: str) -> dict[str, Any]:
        brief = await self._require(brief_id)
        job_ids = await self._brief_job_ids(brief_id)
        statuses: dict[str, int] = {
            "total": len(job_ids),
            "completed": 0,
            "running": 0,
            "failed": 0,
        }
        for job_id in job_ids:
            job = await self.job_repository.get_job(job_id) if self.job_repository else None
            state = getattr(job, "status", None) if job else None
            if state == "succeeded":
                statuses["completed"] += 1
            elif state in {"failed", "cancelled", "timed_out"}:
                statuses["failed"] += 1
            else:
                statuses["running"] += 1
        return {
            "brief_id": brief_id,
            "status": brief.status,
            "total_jobs": statuses["total"],
            "completed_jobs": statuses["completed"],
            "running_jobs": statuses["running"],
            "failed_jobs": statuses["failed"],
            "jobs_by_kind": await self._jobs_by_kind(brief_id),
        }

    async def results(self, brief_id: str) -> dict[str, Any]:
        await self._require(brief_id)
        groups: dict[str, list[dict[str, Any]]] = {}
        for job_id in await self._brief_job_ids(brief_id):
            job = await self.job_repository.get_job(job_id) if self.job_repository else None
            role = ""
            platform = None
            if job is not None:
                role = (getattr(job, "input_json", {}) or {}).get("role", "")
                if "video_" in role or "caption_" in role:
                    platform = role.split("_")[-1]
            assets = []
            if self.asset_repository is not None:
                assets = await self.asset_repository.output_assets_for_job(job_id)
            asset_id = None
            file_path = None
            if assets:
                _relation, asset = assets[0]
                asset_id = asset.id
                file_path = getattr(asset, "file_path", None) or getattr(asset, "path", None)
            key = platform or (role if role else "other")
            groups.setdefault(key, []).append(
                {
                    "job_id": job_id,
                    "role": role,
                    "platform": platform,
                    "asset_id": asset_id,
                    "file_path": file_path,
                    "status": getattr(job, "status", None) if job else None,
                }
            )
        return {"brief_id": brief_id, "groups": groups}

    # --- A3 adapt -----------------------------------------------------------------------

    async def adapt_asset(
        self, asset_id: str, platform: str, output_dir: str | None
    ) -> dict[str, Any]:
        if self.asset_repository is None:
            raise RuntimeError("asset repository not configured")
        asset = await self.asset_repository.get(asset_id)
        if asset is None:
            from pixelle_video.media_assets.service import AssetNotFoundError

            raise AssetNotFoundError("asset not found")
        path = getattr(asset, "file_path", None) or getattr(asset, "path", None)
        if not path:
            raise RuntimeError("asset has no local file path")
        variant = self.platform_adapter.adapt(path, platform, output_dir=output_dir)
        return {
            "asset_id": asset_id,
            "platform": platform,
            "variant": {
                "path": variant.path,
                "width": variant.width,
                "height": variant.height,
                "format": variant.format,
            },
        }

    # --- A4 package / download -------------------------------------------------------------

    async def package(self, brief_id: str, platforms: list[str]) -> dict[str, Any]:
        if self.delivery_packager is None:
            raise RuntimeError("delivery packager not configured")
        await self._require(brief_id)
        resolved_assets: dict[str, list[tuple[Any, Any]]] = {}
        if self.require_complete_outputs:
            job_ids = await self._brief_job_ids(brief_id)
            if not job_ids:
                raise ProductDeliveryNotReadyError("no production jobs exist for this brief")
            failures = await self._failed_jobs(brief_id)
            if failures:
                await self._write_failures(brief_id, failures)
                raise ProductDeliveryNotReadyError("one or more production jobs failed")
            incomplete: list[str] = []
            missing_outputs: list[str] = []
            for job_id in job_ids:
                job = await self.job_repository.get_job(job_id) if self.job_repository else None
                if job is None or getattr(job, "status", None) != "succeeded":
                    incomplete.append(job_id)
                    continue
                if getattr(job, "executor_kind", None) == "llm_caption":
                    continue
                rows = (
                    await self.asset_repository.output_assets_for_job(job_id)
                    if self.asset_repository is not None
                    else []
                )
                materialized: list[tuple[Any, Any]] = []
                for relation, asset in rows:
                    path = self.asset_path_resolver(asset) if self.asset_path_resolver else None
                    if path is not None:
                        setattr(asset, "local_path", str(path))
                        materialized.append((relation, asset))
                if not materialized:
                    missing_outputs.append(job_id)
                else:
                    resolved_assets[job_id] = materialized
            if incomplete:
                raise ProductDeliveryNotReadyError("production jobs are not terminal-success")
            if missing_outputs:
                raise ProductDeliveryNotReadyError(
                    "successful media jobs have no durable output assets"
                )

        qc_job_id = await self._resolve_qc_job(brief_id)
        if qc_job_id is not None:
            self.delivery_packager.qc_job_id = qc_job_id
        captions = await self._collect_captions(brief_id)
        if self.require_complete_outputs:
            for platform in platforms:
                caption = captions.get(platform)
                if not caption or caption.get("status") != "succeeded" or not caption.get("text"):
                    raise ProductDeliveryNotReadyError(
                        f"successful caption output is missing for platform '{platform}'"
                    )
        payload = await self.delivery_packager.package(
            brief_id,
            platforms,
            captions=captions,
            resolved_assets=resolved_assets,
        )
        await self.repository.update_status(brief_id, "completed")
        return payload

    async def _collect_captions(self, brief_id: str) -> dict[str, dict[str, Any]]:
        """Extract real caption text from successful caption job outputs.

        Caption jobs persist their text inline in ``output_metadata`` (via the
        ``llm_caption`` processor), so no external file path is required.
        """

        captions: dict[str, dict[str, Any]] = {}
        for job_id in await self._brief_job_ids(brief_id):
            job = await self.job_repository.get_job(job_id) if self.job_repository else None
            if job is None:
                continue
            role = ((getattr(job, "input_json", {}) or {}).get("role", "")) or ""
            if not role.startswith("caption_"):
                continue
            platform = role[len("caption_") :]
            text = None
            for meta in getattr(job, "output_metadata", None) or []:
                if meta.get("media_type") == "text" and meta.get("content"):
                    text = meta["content"]
                    break
            captions[platform] = {
                "text": text,
                "job_id": job_id,
                "status": getattr(job, "status", None),
            }
        return captions

    async def _failed_jobs(self, brief_id: str) -> list[dict[str, Any]]:
        """List the brief's terminal-failed jobs so delivery never looks complete."""

        failures: list[dict[str, Any]] = []
        for job_id in await self._brief_job_ids(brief_id):
            job = await self.job_repository.get_job(job_id) if self.job_repository else None
            if job is None:
                continue
            state = getattr(job, "status", None)
            if state in {"failed", "cancelled", "timed_out"}:
                failures.append(
                    {
                        "job_id": job_id,
                        "role": ((getattr(job, "input_json", {}) or {}).get("role", "")) or "",
                        "status": state,
                        "error": getattr(job, "error_message", None),
                    }
                )
        return failures

    async def _write_failures(self, brief_id: str, failures: list[dict[str, Any]]) -> None:
        brief = await self._require(brief_id)
        exports_root = (
            Path(self.delivery_packager.exports_root)
            if self.delivery_packager
            else Path("exports")
        )
        delivery_root = (
            exports_root / (brief.project_id or "default-project") / brief_id / "delivery"
        )
        delivery_root.mkdir(parents=True, exist_ok=True)
        (delivery_root / "failures.json").write_text(
            json.dumps({"failed_jobs": failures}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _resolve_qc_job(self, brief_id: str) -> str | None:
        job_ids = await self._brief_job_ids(brief_id)
        if not job_ids:
            return None
        # Prefer the main image job (the brief's representative asset).
        for job_id in job_ids:
            job = await self.job_repository.get_job(job_id) if self.job_repository else None
            role = ((getattr(job, "input_json", {}) or {}).get("role", "") if job else "") or ""
            if role == "main_image":
                return job_id
        return job_ids[0]

    async def package_status(self, brief_id: str) -> dict[str, Any]:
        brief = await self._require(brief_id)
        exports_root = (
            Path(self.delivery_packager.exports_root) if self.delivery_packager else Path("exports")
        )
        delivery_root = (
            exports_root / (brief.project_id or "default-project") / brief_id / "delivery"
        )
        platforms = (
            [entry.name for entry in delivery_root.iterdir() if entry.is_dir()]
            if delivery_root.exists()
            else []
        )
        return {
            "brief_id": brief_id,
            "packaged": delivery_root.exists(),
            "delivery_root": str(delivery_root) if delivery_root.exists() else None,
            "platforms": platforms,
        }

    def download_zip(self, brief_id: str, project_id: str | None) -> str | None:
        if self.delivery_packager is None:
            return None
        return self.delivery_packager.zip_delivery(brief_id, project_id)

    # --- helpers ----------------------------------------------------------------------------

    async def _require(self, brief_id: str):
        brief = await self.repository.get_brief(brief_id)
        if brief is None:
            from pixelle_video.products.repository import ProductBriefNotFoundError

            raise ProductBriefNotFoundError("product brief not found")
        return brief

    async def _brief_job_ids(self, brief_id: str) -> list[str]:
        if self.job_repository is None:
            return []
        rows = await self.job_repository.list_jobs(limit=1000, offset=0)
        return [
            job.job_id
            for job in rows
            if (getattr(job, "input_json", {}) or {}).get("brief_id") == brief_id
        ]

    async def _jobs_by_kind(self, brief_id: str) -> dict[str, int]:
        counts: dict[str, int] = {"image": 0, "video": 0, "caption": 0}
        for job_id in await self._brief_job_ids(brief_id):
            job = await self.job_repository.get_job(job_id) if self.job_repository else None
            role = ((getattr(job, "input_json", {}) or {}).get("role", "") if job else "") or ""
            if role.startswith("main_image") or role.startswith("scene_image"):
                counts["image"] += 1
            elif role.startswith("ad_video"):
                counts["video"] += 1
            elif role.startswith("caption"):
                counts["caption"] += 1
        return counts
