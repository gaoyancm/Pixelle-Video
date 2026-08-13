"""Phase 10 T1 probe: image-upload → first-frame I2V wiring (local, no GPU).

Validates the full reference-image chain against real services on a temp
SQLite + local asset store:

  1. Real PNG bytes -> AssetService.upload -> asset_id (INPUT asset)
  2. 04-E Content Plan -> create_brief_from_plan(reference_images) ->
     brief keeps reference_images_json as the real asset id and stores the
     plan trace on the dedicated plan_id column
  3. start_production -> ad video jobs use the I2V workflow and link the
     input asset via media_job_assets

Usage:
  .venv/Scripts/python.exe probe10_i2v.py
"""

from __future__ import annotations

import asyncio
import io
import tempfile
from pathlib import Path

from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models  # noqa: F401  (projects table)
from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_assets.service import AssetService
from pixelle_video.media_assets.store import LocalAssetStore
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.orchestration.repository import ContentPlanRepository
from pixelle_video.products.ad_engine import AdProductionEngine, I2V_WORKFLOW
from pixelle_video.products.brief_mapper import BriefMapper
from pixelle_video.products.repository import ProductBriefRepository
from api.services.products import ProductApplicationService


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color=(200, 80, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(root / 'probe.db').as_posix()}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        asset_repository = AssetRepository(factory)
        asset_store = LocalAssetStore(root / "assets")
        asset_service = AssetService(
            asset_repository, asset_store, max_upload_size=50 * 1024 * 1024
        )
        brief_repository = ProductBriefRepository(factory)
        job_repository = MediaJobRepository(factory)
        management_repository = ManagementRepository(factory)
        plan_repository = ContentPlanRepository(factory)

        # 1. upload real bytes
        asset, created = await asset_service.upload(
            io.BytesIO(_png_bytes()),
            filename="product.png",
            mime_type="image/png",
            idempotency_key="probe-upload-1",
        )
        assert created and asset.kind == "input" and asset.state == "available"
        print(f"[1] upload OK -> asset_id={asset.id}")

        # 2. plan -> brief (with reference image)
        plan = await plan_repository.create_plan(
            request_text="手工皮具钱包，Etsy主图+TikTok广告",
            intent="product_ad",
            plan_json={
                "summary": "为手工皮具钱包生成广告",
                "target_audience": "25-45岁",
                "platforms": ["etsy", "tiktok"],
                "creative_directions": [{"angle": "品质感"}],
            },
        )
        service = ProductApplicationService(
            brief_repository,
            ad_engine=AdProductionEngine(
                brief_repository, management_repository, job_repository
            ),
            asset_repository=asset_repository,
            job_repository=job_repository,
            plan_repository=plan_repository,
            brief_mapper=BriefMapper(),
        )
        payload = await service.create_brief_from_plan(
            plan.id, reference_images=[asset.id]
        )
        brief = await brief_repository.get_brief(payload["brief_id"])
        assert brief.plan_id == plan.id, "plan trace must use the plan_id column"
        assert brief.reference_images_json == [asset.id], (
            "reference_images_json must hold the real reference image"
        )
        print(
            f"[2] brief OK -> plan_id={brief.plan_id[:8]} "
            f"reference_images={brief.reference_images_json}"
        )

        # 3. production -> I2V jobs linking the input asset
        production = await service.ad_engine.start_production(
            brief.id, executor_kind_override="mock_executor"
        )
        video_jobs = production["jobs"]["video"]
        assert len(video_jobs) == 2
        for job_id in video_jobs:
            job = await job_repository.get_job(job_id)
            assert job.workflow_type == I2V_WORKFLOW
            assert job.input_assets_json == [
                {"asset_id": asset.id, "role": "input_image"}
            ]
            linked = await asset_repository.input_assets_for_job(job_id)
            assert [a.id for a in linked] == [asset.id]
        print(f"[3] I2V jobs OK -> {len(video_jobs)} video jobs, workflow={I2V_WORKFLOW}")

        await engine.dispose()
    print("probe10_i2v: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
