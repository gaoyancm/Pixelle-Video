"""Phase 10 task 5: product ad pipeline fake E2E.

Exercises the public API surface end-to-end with fake executors: upload a
reference image -> create brief -> produce -> worker processes caption jobs ->
package -> download zip. Asserts captions come from the successful caption job
output (or an explicitly-marked fallback) and that partial failures are visible.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_product_service
from api.routers.products import router as products_router
from api.services.products import ProductApplicationService
from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_assets.contracts import new_asset_id
from pixelle_video.media_assets.models import MediaAsset
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_assets.service import AssetService
from pixelle_video.media_assets.store import LocalAssetStore
from pixelle_video.media_jobs.dispatcher import DispatchingJobProcessor, LLMCaptionJobProcessor
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus
from pixelle_video.media_jobs.worker import MediaJobWorker
from pixelle_video.products.ad_engine import AdProductionEngine
from pixelle_video.products.delivery import DeliveryPackager
from pixelle_video.products.repository import ProductBriefRepository


class _FakeComfyUIProcessor:
    """Mark a private_comfyui job succeeded without touching a GPU."""

    def __init__(self, repository: MediaJobRepository, asset_service: AssetService):
        self.repository = repository
        self.asset_service = asset_service

    async def process(self, job, lease) -> None:
        for target in (JobStatus.SUBMITTING, JobStatus.RUNNING):
            await lease.mutate(
                lambda s, v, t=target: self.repository.transition_owned(
                    job.job_id,
                    expected_status=s,
                    expected_version=v,
                    lease_owner=lease.worker_id,
                    target_status=t,
                )
            )
        content = b"\x89PNG\r\n\x1a\n" + b"fake-generated-media"
        asset = await self.asset_service.register_generated_bytes(
            content, filename=f"{job.job_id}.png"
        )
        metadata = {
            "output_id": asset.id,
            "media_type": asset.media_type,
            "relative_path": asset.object_key,
            "size": asset.size_bytes,
            "mime_type": asset.mime_type,
            "sha256": asset.sha256,
        }
        await lease.mutate(
            lambda _s, v: self.asset_service.repository.register_output_group(
                job_id=job.job_id,
                lease_owner=lease.worker_id,
                expected_version=v,
                assets=[asset],
                roles=["primary"],
                output_metadata=[metadata],
            )
        )
        await lease.mutate(
            lambda s, v: self.repository.transition_owned(
                job.job_id,
                expected_status=s,
                expected_version=v,
                lease_owner=lease.worker_id,
                target_status=JobStatus.SUCCEEDED,
            )
        )


async def _fake_llm(prompt: str) -> str:
    return f"真实文案输出：{prompt}"


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'e2e.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    job_repository = MediaJobRepository(factory)
    management_repository = ManagementRepository(factory)
    asset_repository = AssetRepository(factory)
    asset_store = LocalAssetStore(tmp_path / "asset-store")
    ad_engine = AdProductionEngine(
        brief_repository, management_repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    exports_root = tmp_path / "exports"
    packager = DeliveryPackager(brief_repository, exports_root=str(exports_root))
    service = ProductApplicationService(
        brief_repository,
        ad_engine=ad_engine,
        asset_repository=asset_repository,
        job_repository=job_repository,
        delivery_packager=packager,
        asset_path_resolver=lambda asset: asset_store.local_path(asset.object_key),
    )
    try:
        yield (
            factory,
            brief_repository,
            job_repository,
            service,
            exports_root,
            tmp_path,
            asset_repository,
        )
    finally:
        await engine.dispose()


async def _run_worker(job_repository: MediaJobRepository, tmp_path: Path, *, llm=_fake_llm) -> None:
    """Run the production dispatcher once over all queued jobs."""

    asset_service = AssetService(
        AssetRepository(job_repository._session_factory),
        LocalAssetStore(tmp_path / "asset-store"),
        max_upload_size=10 * 1024 * 1024,
    )
    dispatcher = DispatchingJobProcessor(
        job_repository,
        {
            "private_comfyui": _FakeComfyUIProcessor(job_repository, asset_service),
            "llm_caption": LLMCaptionJobProcessor(
                job_repository, llm_caller=llm, managed_output_root=tmp_path / "outputs"
            ),
        },
    )
    worker = MediaJobWorker(
        job_repository, dispatcher, worker_id="e2e", lease_seconds=60, heartbeat_seconds=1
    )
    # Claim and process repeatedly until no queued jobs remain.
    for _ in range(20):
        processed = await worker.run_once()
        if processed == 0:
            break


async def _make_input_asset(asset_repository: AssetRepository) -> str:
    aid = new_asset_id()
    asset = MediaAsset(
        id=aid,
        kind="input",
        state="available",
        backend="local",
        object_key=f"inputs/{aid}.png",
        original_filename="ref.png",
        media_type="image",
        mime_type="image/png",
        size_bytes=10,
        sha256="a" * 64,
        source="upload",
    )
    persisted, _ = await asset_repository.create(asset)
    return persisted.id


async def _brief_with_reference(
    brief_repository: ProductBriefRepository, reference_images: list[str]
) -> str:
    brief = await brief_repository.create_brief(
        product_name="手工皮具钱包",
        description="意大利头层牛皮手工缝制",
        selling_points=["头层牛皮", "手工缝制"],
        platforms=["tiktok"],
        project_id="project-x",
        reference_images=reference_images,
    )
    return brief.id


async def test_products_e2e_caption_comes_from_real_job(env) -> None:
    _f, brief_repository, job_repository, service, exports_root, tmp_path, asset_repository = env
    asset_id = await _make_input_asset(asset_repository)
    brief_id = await _brief_with_reference(brief_repository, [asset_id])
    await service.ad_engine.start_production(brief_id)
    await _run_worker(job_repository, tmp_path)

    payload = await service.package(brief_id, ["tiktok"])

    platform_dir = exports_root / "project-x" / brief_id / "delivery" / "tiktok"
    captions = (platform_dir / "captions.txt").read_text(encoding="utf-8")
    assert "真实文案输出" in captions
    source = json.loads((platform_dir / "captions_source.json").read_text(encoding="utf-8"))
    assert source["source"] == "caption_job"
    assert source["job_id"]
    assert payload["failures"] == [] if "failures" in payload else True


async def test_products_e2e_failed_caption_blocks_delivery(env) -> None:
    _f, brief_repository, job_repository, service, exports_root, tmp_path, _ar = env

    async def broken_llm(_prompt: str) -> str:
        raise RuntimeError("provider down")

    brief_id = await _brief_with_reference(brief_repository, [])
    await service.ad_engine.start_production(brief_id)
    await _run_worker(job_repository, tmp_path, llm=broken_llm)

    from api.services.products import ProductDeliveryNotReadyError

    with pytest.raises(ProductDeliveryNotReadyError):
        await service.package(brief_id, ["tiktok"])
    brief = await brief_repository.get_brief(brief_id)
    assert brief.status == "processing"


async def test_products_e2e_zip_contains_caption_and_metadata(env) -> None:
    _f, brief_repository, job_repository, service, exports_root, tmp_path, asset_repository = env
    asset_id = await _make_input_asset(asset_repository)
    brief_id = await _brief_with_reference(brief_repository, [asset_id])
    await service.ad_engine.start_production(brief_id)
    await _run_worker(job_repository, tmp_path)
    await service.package(brief_id, ["tiktok"])

    zip_path = service.download_zip(brief_id, "project-x")
    assert zip_path and Path(zip_path).exists()
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert any(name.endswith("captions.txt") for name in names)
        assert any(name.endswith("metadata.json") for name in names)
        assert any(name.endswith("captions_source.json") for name in names)
        assert any(name.endswith(".png") for name in names)


async def test_products_e2e_partial_failure_is_visible(env) -> None:
    _f, brief_repository, job_repository, service, exports_root, tmp_path, _ar = env
    brief_id = await _brief_with_reference(brief_repository, [])

    async def broken_llm(_prompt: str) -> str:
        raise RuntimeError("provider down")

    await service.ad_engine.start_production(brief_id)
    # The caption job fails; image/video jobs still succeed via the fake executor.
    await _run_worker(job_repository, tmp_path, llm=broken_llm)

    from api.services.products import ProductDeliveryNotReadyError

    with pytest.raises(ProductDeliveryNotReadyError):
        await service.package(brief_id, ["tiktok"])
    # The failure manifest is written into the delivery root.
    manifest = exports_root / "project-x" / brief_id / "delivery" / "failures.json"
    assert manifest.is_file()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["failed_jobs"]


@pytest.fixture
async def api_client(env):
    _f, _br, _jr, service, _exports, _tmp, _ar = env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _br, _jr, service, _tmp


async def test_products_e2e_public_api(api_client) -> None:
    client, brief_repository, job_repository, service, tmp_path = api_client
    # 1. create brief via public API
    created = await client.post(
        "/api/products/briefs",
        json={
            "product_name": "测试商品",
            "description": "测试描述",
            "selling_points": ["卖点A"],
            "platforms": ["tiktok"],
            "project_id": "project-api",
        },
    )
    assert created.status_code == 201
    brief_id = created.json()["id"]

    # 2. start production via public API
    produced = await client.post(f"/api/products/briefs/{brief_id}/confirm")
    assert produced.status_code == 200

    # 3. worker processes jobs
    await _run_worker(job_repository, tmp_path)

    # 4. poll progress
    progress = await client.get(f"/api/products/briefs/{brief_id}/progress")
    assert progress.status_code == 200
    body = progress.json()
    assert body["completed_jobs"] == body["total_jobs"]

    # 5. package + zip
    packaged = await client.post(
        f"/api/products/briefs/{brief_id}/package", json={"platforms": ["tiktok"]}
    )
    assert packaged.status_code == 200
    assert packaged.json()["product_name"] == "测试商品"
