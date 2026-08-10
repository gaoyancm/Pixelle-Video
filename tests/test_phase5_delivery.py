"""Phase 05 A4 delivery package tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_product_service
from api.routers.products import router as products_router
from api.services.products import ProductApplicationService
from pixelle_video.management.repository import ManagementRepository
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.products.ad_engine import AdProductionEngine
from pixelle_video.products.delivery import DeliveryPackager
from pixelle_video.products.repository import ProductBriefRepository


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a4.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    job_repository = MediaJobRepository(factory)
    management_repository = ManagementRepository(factory)
    asset_repository = AssetRepository(factory)
    ad_engine = AdProductionEngine(brief_repository, management_repository, job_repository)
    exports_root = tmp_path / "exports"
    packager = DeliveryPackager(brief_repository, exports_root=str(exports_root))
    service = ProductApplicationService(
        brief_repository,
        ad_engine=ad_engine,
        asset_repository=asset_repository,
        job_repository=job_repository,
        delivery_packager=packager,
    )
    try:
        yield (
            factory,
            brief_repository,
            job_repository,
            management_repository,
            service,
            exports_root,
        )
    finally:
        await engine.dispose()


async def _brief(repository: ProductBriefRepository) -> str:
    brief = await repository.create_brief(
        product_name="手工皮具钱包",
        description="意大利头层牛皮手工缝制",
        selling_points=["头层牛皮", "手工缝制"],
        platforms=["tiktok", "instagram"],
        project_id="project-x",
    )
    return brief.id


async def test_package_creates_platform_directories(env) -> None:
    _factory, repository, _jobs, _management, service, exports_root = env
    brief_id = await _brief(repository)
    payload = await service.package(brief_id, ["tiktok", "instagram"])
    assert payload["brief_id"] == brief_id
    assert set(payload["platforms"]) == {"tiktok", "instagram"}
    for platform in ("tiktok", "instagram"):
        platform_dir = exports_root / "project-x" / brief_id / "delivery" / platform
        assert platform_dir.is_dir()


async def test_package_writes_captions_and_hashtags(env) -> None:
    _factory, repository, _jobs, _management, service, exports_root = env
    brief_id = await _brief(repository)
    await service.package(brief_id, ["tiktok"])
    platform_dir = exports_root / "project-x" / brief_id / "delivery" / "tiktok"
    captions = (platform_dir / "captions.txt").read_text(encoding="utf-8")
    hashtags = (platform_dir / "hashtags.txt").read_text(encoding="utf-8")
    assert "手工皮具钱包" in captions
    assert "#手工皮具钱包" in hashtags


async def test_package_metadata_contains_full_trace(env) -> None:
    _factory, repository, _jobs, _management, service, exports_root = env
    brief_id = await _brief(repository)
    await service.package(brief_id, ["instagram"])
    platform_dir = exports_root / "project-x" / brief_id / "delivery" / "instagram"
    metadata = json.loads((platform_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["brief_id"] == brief_id
    assert metadata["product_name"] == "手工皮具钱包"
    assert metadata["platform"] == "instagram"
    assert metadata["prompt"]["selling_points"] == ["头层牛皮", "手工缝制"]
    assert metadata["model"]
    assert metadata["parameters"]["platform"] == "instagram"


async def test_package_sets_brief_completed(env) -> None:
    _factory, repository, _jobs, _management, service, _exports = env
    brief_id = await _brief(repository)
    await service.package(brief_id, ["tiktok"])
    brief = await repository.get_brief(brief_id)
    assert brief.status == "completed"


async def test_package_status(env) -> None:
    _factory, repository, _jobs, _management, service, _exports = env
    brief_id = await _brief(repository)
    before = await service.package_status(brief_id)
    assert before["packaged"] is False
    await service.package(brief_id, ["tiktok"])
    after = await service.package_status(brief_id)
    assert after["packaged"] is True
    assert "tiktok" in after["platforms"]


async def test_zip_delivery_creates_archive(env) -> None:
    _factory, repository, _jobs, _management, service, exports_root = env
    brief_id = await _brief(repository)
    await service.package(brief_id, ["tiktok"])
    zip_path = service.download_zip(brief_id, "project-x")
    assert zip_path is not None
    assert Path(zip_path).exists()
    import zipfile

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert any("captions.txt" in name for name in names)
        assert any("metadata.json" in name for name in names)


async def test_qc_runs_before_delivery(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'qc.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    brief_repository = ProductBriefRepository(factory)
    qc_calls: list[str] = []

    def fake_qc(job_id: str):
        qc_calls.append(job_id)
        return {"decision": "pass", "job_id": job_id}

    packager = DeliveryPackager(
        brief_repository,
        qc_job_id="job-qc-1",
        qc_runner=fake_qc,
        exports_root=str(tmp_path / "exports-qc"),
    )
    service = ProductApplicationService(brief_repository, delivery_packager=packager)
    brief_id = await _brief(brief_repository)
    await service.package(brief_id, ["meta"])
    assert qc_calls == ["job-qc-1"]
    await engine.dispose()


@pytest.fixture
async def api_client(env):
    _factory, _repository, _jobs, _management, service, _exports = env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_package_and_status(api_client) -> None:
    client, _factory, repository, _service = api_client
    brief_id = await _brief(repository)
    packaged = await client.post(
        f"/api/products/briefs/{brief_id}/package",
        json={"platforms": ["tiktok"]},
    )
    assert packaged.status_code == 200
    body = packaged.json()
    assert "tiktok" in body["platforms"]
    assert body["product_name"] == "手工皮具钱包"

    status = await client.get(f"/api/products/briefs/{brief_id}/package/status")
    assert status.status_code == 200
    assert status.json()["packaged"] is True


async def test_package_resolves_brief_qc_job_and_populates_metadata(env) -> None:
    """D2: in production wiring the package flow resolves the brief's QC
    job and writes a non-null qc block into metadata.json."""
    _factory, repository, job_repository, management_repository, service, exports_root = env
    brief_id = await _brief(repository)

    def fake_qc(job_id: str):
        return {"decision": "pass", "job_id": job_id}

    # Wire the packager's QC runner (as the production dependency does).
    service.delivery_packager.qc_runner = fake_qc
    # Start production so the brief has jobs to resolve for QC.
    await service.ad_engine.start_production(brief_id, executor_kind_override="mock_executor")
    payload = await service.package(brief_id, ["tiktok"])
    metadata = payload["platforms"]["tiktok"]["metadata"]
    assert metadata["qc"] is not None
    assert metadata["qc"]["decision"] == "pass"
    assert metadata["qc"]["job_id"] is not None
