from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.app import app
from api.dependencies import get_media_asset_service, get_media_job_service
from api.services.media_jobs import MediaJobApplicationService
from pixelle_video.config.schema import MediaJobsConfig
from pixelle_video.media_assets import AssetRepository, AssetService, LocalAssetStore
from pixelle_video.media_assets.models import MediaJobAsset
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


@pytest.fixture
async def api_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    assets = AssetService(
        AssetRepository(factory), LocalAssetStore(tmp_path / "store"), max_upload_size=1024
    )
    jobs = MediaJobApplicationService(
        MediaJobRepository(factory),
        MediaJobsConfig(enabled=True),
        assets,
    )
    app.dependency_overrides[get_media_asset_service] = lambda: assets
    app.dependency_overrides[get_media_job_service] = lambda: jobs
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, assets, factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_asset_api_upload_get_list_content_delete_and_redaction(api_context):
    client, _, _ = api_context
    upload = await client.post(
        "/api/assets",
        headers={"Idempotency-Key": "upload-1"},
        files={"file": ("client.png", PNG, "image/png")},
    )
    assert upload.status_code == 201
    body = upload.json()
    assert set(body).isdisjoint({"backend", "object_key", "path"})
    asset_id = body["asset_id"]

    replay = await client.post(
        "/api/assets",
        headers={"Idempotency-Key": "upload-1"},
        files={"file": ("client.png", PNG, "image/png")},
    )
    assert replay.status_code == 200
    assert replay.json()["asset_id"] == asset_id

    fetched = await client.get(f"/api/assets/{asset_id}")
    assert fetched.status_code == 200
    listed = await client.get("/api/assets", params={"kind": "input", "state": "available"})
    assert [item["asset_id"] for item in listed.json()["items"]] == [asset_id]
    content = await client.get(f"/api/assets/{asset_id}/content")
    assert content.status_code == 200
    assert content.content == PNG
    assert content.headers["content-length"] == str(len(PNG))

    deleted = await client.delete(f"/api/assets/{asset_id}")
    assert deleted.status_code == 200
    assert deleted.json()["state"] == "deleted"
    assert (await client.get(f"/api/assets/{asset_id}/content")).status_code == 409


@pytest.mark.asyncio
async def test_asset_api_stable_errors_do_not_leak_paths(api_context):
    client, _, _ = api_context
    invalid = await client.post(
        "/api/assets",
        headers={"Idempotency-Key": "upload-1"},
        files={"file": ("fake.png", b"bad", "image/png")},
    )
    assert invalid.status_code == 422
    assert "D:\\" not in invalid.text
    missing = await client.get("/api/assets/00000000-0000-4000-8000-000000000099")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_new_job_requires_real_available_asset_and_creates_relation(api_context):
    client, _, factory = api_context
    upload = await client.post(
        "/api/assets",
        headers={"Idempotency-Key": "upload-job"},
        files={"file": ("client.png", PNG, "image/png")},
    )
    asset_id = upload.json()["asset_id"]
    request = {
        "workflow": "gpu_4090_wan21_i2v_33f",
        "asset_id": asset_id,
        "parameters": {"prompt": "animate this image"},
    }
    created = await client.post(
        "/api/media/jobs",
        headers={"Idempotency-Key": "job-1"},
        json=request,
    )
    assert created.status_code == 201
    async with factory() as session:
        relation = (
            await session.execute(
                select(MediaJobAsset).where(
                    MediaJobAsset.job_id == created.json()["job_id"]
                )
            )
        ).scalar_one()
    assert relation.asset_id == asset_id
    assert relation.direction == "input"
    assert relation.position == 0

    path_value = await client.post(
        "/api/media/jobs",
        headers={"Idempotency-Key": "job-path"},
        json={**request, "asset_id": "legacy/input.png"},
    )
    assert path_value.status_code == 422


@pytest.mark.asyncio
async def test_retry_copies_only_input_relations(api_context):
    client, _, factory = api_context
    upload = await client.post(
        "/api/assets",
        headers={"Idempotency-Key": "upload-retry"},
        files={"file": ("client.png", PNG, "image/png")},
    )
    asset_id = upload.json()["asset_id"]
    created = await client.post(
        "/api/media/jobs",
        headers={"Idempotency-Key": "job-retry-source"},
        json={
            "workflow": "gpu_4090_wan21_i2v_33f",
            "asset_id": asset_id,
            "parameters": {"prompt": "animate this image"},
        },
    )
    job_id = created.json()["job_id"]
    from pixelle_video.media_jobs.models import MediaJob

    async with factory() as session:
        async with session.begin():
            source = await session.get(MediaJob, job_id)
            source.status = "failed"
            source.error_category = "remote_failed"
    retry = await client.post(
        f"/api/media/jobs/{job_id}/retry",
        headers={"Idempotency-Key": "retry-1"},
    )
    assert retry.status_code == 201
    async with factory() as session:
        relations = list(
            (
                await session.execute(
                    select(MediaJobAsset).where(
                        MediaJobAsset.job_id == retry.json()["job_id"]
                    )
                )
            ).scalars()
        )
    assert [(item.asset_id, item.direction) for item in relations] == [(asset_id, "input")]
