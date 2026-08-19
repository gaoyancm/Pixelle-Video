"""Phase 10 task 7: anime pipeline real-binding fake E2E.

Asserts the anime UI is no longer a demo shell and the public API runs a real
project -> episode -> scenes -> shots -> plan -> generate -> progress ->
consistency-report flow end to end.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_anime_service
from api.routers.anime import router as anime_router
from api.services.anime import AnimeApplicationService
from pixelle_video.anime.packager import AnimePackager
from pixelle_video.anime.repository import AnimeRepository
from pixelle_video.anime.shot_engine import ShotProductionEngine
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_assets.service import AssetService
from pixelle_video.media_assets.store import LocalAssetStore
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus
from pixelle_video.media_jobs.worker import MediaJobWorker


class _FakeShotProcessor:
    def __init__(self, repository, assets):
        self.repository = repository
        self.assets = assets

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
        asset = await self.assets.register_generated_bytes(
            b"\x89PNG\r\n\x1a\n" + b"anime-shot", filename=f"{job.job_id}.png"
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
            lambda _s, v: self.assets.repository.register_output_group(
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


def test_anime_page_has_no_demo_or_fabricated_logic() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "pixelle_video"
        / "web"
        / "pages"
        / "anime.py"
    ).read_text(encoding="utf-8")
    assert "/projects/demo" not in source, "hard-coded demo project must be removed"
    assert "模拟分镜" not in source, "fabricated storyboard preview must be removed"
    assert "total - 3" not in source, "computed progress must be removed"
    assert 'len(c["name"])' not in source and 'len(c[\'name\'])' not in source, (
        "name-length consistency must be removed"
    )
    assert "scenes_data = [" not in source, "mock scenes data must be removed"


@pytest.fixture
async def api_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a7.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    job_repository = MediaJobRepository(factory)
    asset_repository = AssetRepository(factory)
    asset_service = AssetService(
        asset_repository,
        LocalAssetStore(tmp_path / "asset-store"),
        max_upload_size=10 * 1024 * 1024,
    )
    shot_engine = ShotProductionEngine(
        repository,
        job_repository,
        node_selector=lambda _workflow: "test-node",
        asset_repository=asset_repository,
    )
    service = AnimeApplicationService(
        repository,
        job_repository=job_repository,
        shot_engine=shot_engine,
        packager=AnimePackager(
            repository,
            asset_repository,
            asset_path_resolver=lambda asset: asset_service.store.local_path(asset.object_key),
            exports_root=tmp_path / "exports",
        ),
    )
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            yield client, repository, job_repository, asset_service, tmp_path
        finally:
            await engine.dispose()


async def test_anime_public_api_full_flow(api_env) -> None:
    client, repository, job_repository, asset_service, tmp_path = api_env

    # 1. create anime project
    project = await client.post(
        "/api/anime/projects",
        json={"project_id": "project-x", "world_setting": "武侠世界", "style_profile": "动画"},
    )
    assert project.status_code == 201
    anime_project_id = project.json()["id"]

    # 2. create episode
    episode = await client.post(
        "/api/anime/episodes",
        json={"anime_project_id": anime_project_id, "season_no": 1, "episode_no": 1,
              "title": "第一集"},
    )
    assert episode.status_code == 201
    episode_id = episode.json()["id"]

    # 3. create two scenes
    scene_ids = []
    for scene_no in (1, 2):
        scene = await client.post(
            f"/api/anime/episodes/{episode_id}/scenes",
            json={"episode_id": episode_id, "scene_no": scene_no,
                  "description": f"第 {scene_no} 场"},
        )
        assert scene.status_code == 201
        scene_ids.append(scene.json()["id"])

    # 4. create at least 4 shots across the scenes
    shot_ids = []
    for scene_id in scene_ids:
        for shot_no in (1, 2):
            shot = await client.post(
                f"/api/anime/scenes/{scene_id}/shots",
                json={"scene_id": scene_id, "shot_no": shot_no, "duration_sec": 5,
                      "visual_description": f"镜头{shot_no}"},
            )
            assert shot.status_code == 201
            shot_ids.append(shot.json()["id"])
    assert len(shot_ids) >= 4

    # 5. plan + generate + progress
    for scene_id in scene_ids:
        planned = await client.post(f"/api/anime/scenes/{scene_id}/plan")
        assert planned.status_code == 200
        generated = await client.post(f"/api/anime/scenes/{scene_id}/generate")
        assert generated.status_code == 200
        progress = await client.get(f"/api/anime/scenes/{scene_id}/progress")
        assert progress.status_code == 200
        assert "states" in progress.json()

    worker = MediaJobWorker(
        job_repository,
        _FakeShotProcessor(job_repository, asset_service),
        worker_id="anime-e2e",
        lease_seconds=2,
        heartbeat_seconds=0.1,
    )
    for _ in range(10):
        if await worker.run_once() == 0:
            break
    for scene_id in scene_ids:
        progress = await client.get(f"/api/anime/scenes/{scene_id}/progress")
        assert progress.json()["states"]["succeeded"] == 2

    # 6. retry a shot (idempotent retry surface exists)
    retried = await client.post(f"/api/anime/shots/{shot_ids[0]}/retry")
    assert retried.status_code == 200
    await worker.run_once()
    await client.get(f"/api/anime/scenes/{scene_ids[0]}/progress")

    # 7. episode consistency report
    report = await client.get(f"/api/anime/episodes/{episode_id}/consistency-report")
    assert report.status_code == 200
    assert report.json()["consistent"] is True

    packaged = await client.post(f"/api/anime/episodes/{episode_id}/package")
    assert packaged.status_code == 200
    assert len(packaged.json()["files"]) == 4
    downloaded = await client.get(f"/api/anime/episodes/{episode_id}/download")
    assert downloaded.status_code == 200
    archive_path = tmp_path / "episode.zip"
    archive_path.write_bytes(downloaded.content)
    with zipfile.ZipFile(archive_path) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".png")]) == 4
