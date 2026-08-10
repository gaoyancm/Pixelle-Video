"""Phase 06 S2 storyboard and asset generation tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_video_service
from api.routers.videos import router as videos_router
from api.services.videos import VideoApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine
from pixelle_video.videos.storyboard import StoryboardEngine


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    job_repository = MediaJobRepository(factory)
    script_engine = ScriptEngine(repository)
    storyboard_engine = StoryboardEngine(repository, job_repository)
    service = VideoApplicationService(
        repository,
        script_engine=script_engine,
        storyboard_engine=storyboard_engine,
        job_repository=job_repository,
    )
    try:
        yield factory, repository, job_repository, service, storyboard_engine
    finally:
        await engine.dispose()


async def _make_script(repository: VideoScriptRepository) -> str:
    payload = await ScriptEngine(repository).generate_script(
        topic="人工智能如何改变日常生活",
        target_duration=60,
    )
    return payload["id"]


async def test_storyboard_builds_frames_from_scenes(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    script_id = await _make_script(repository)
    payload = await engine.build_storyboard(script_id)
    frames = payload["frames"]
    assert len(frames) == 3
    first = frames[0]
    assert first["index"] == 1
    assert first["image_prompt"]
    assert first["video_prompt"]
    assert first["asset_source"] == "ai_generated"
    assert first["status"] == "pending"
    assert "stock_keywords" in first


async def test_storyboard_sets_status(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    script_id = await _make_script(repository)
    await engine.build_storyboard(script_id)
    script = await repository.get_script(script_id)
    assert script.status == "storyboarding"
    assert "storyboard" in script.script_json


async def test_storyboard_get_returns_frames(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    script_id = await _make_script(repository)
    await engine.build_storyboard(script_id)
    payload = await engine.get_storyboard(script_id)
    assert len(payload["frames"]) == 3


async def test_generate_assets_creates_jobs(env) -> None:
    _factory, repository, job_repository, _service, engine = env
    script_id = await _make_script(repository)
    await engine.build_storyboard(script_id)
    payload = await engine.generate_assets(script_id, executor_kind_override="mock_executor")
    assert len(payload["jobs"]) == 3
    for job_id in payload["jobs"].values():
        job = await job_repository.get_job(job_id)
        assert job is not None
        assert job.input_json["script_id"] == script_id


async def test_generate_assets_updates_frames(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    script_id = await _make_script(repository)
    await engine.build_storyboard(script_id)
    await engine.generate_assets(script_id, executor_kind_override="mock_executor")
    script = await repository.get_script(script_id)
    frames = script.script_json["storyboard"]["frames"]
    assert all(frame["generated_asset_id"] for frame in frames)
    assert all(frame["status"] == "queued" for frame in frames)
    assert script.status == "assets"


async def test_progress_counts_states(env) -> None:
    _factory, repository, job_repository, _service, engine = env
    script_id = await _make_script(repository)
    await engine.build_storyboard(script_id)
    payload = await engine.generate_assets(script_id, executor_kind_override="mock_executor")
    from sqlalchemy import update

    from pixelle_video.media_jobs.models import MediaJob

    first_job = next(iter(payload["jobs"].values()))
    async with job_repository._session_factory() as session:
        async with session.begin():
            await session.execute(
                update(MediaJob).where(MediaJob.job_id == first_job).values(status="succeeded")
            )
    progress = await engine.progress(script_id)
    assert progress["states"]["completed"] == 1
    assert progress["states"]["queued"] == 2


async def test_progress_missing_script_raises(env) -> None:
    _factory, repository, _jobs, _service, engine = env
    from pixelle_video.videos.repository import VideoScriptNotFoundError

    with pytest.raises(VideoScriptNotFoundError):
        await engine.build_storyboard("missing")


@pytest.fixture
async def api_client(env):
    _factory, _repository, _jobs, service, _engine = env
    app = FastAPI()
    app.include_router(videos_router, prefix="/api")
    app.dependency_overrides[get_video_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_storyboard_and_assets_flow(api_client) -> None:
    client, _factory, repository, service = api_client
    script_id = await _make_script(repository)
    storyboard = await client.post(f"/api/videos/scripts/{script_id}/storyboard")
    assert storyboard.status_code == 200
    assert len(storyboard.json()["frames"]) == 3

    fetched = await client.get(f"/api/videos/scripts/{script_id}/storyboard")
    assert fetched.status_code == 200

    assets = await client.post(f"/api/videos/scripts/{script_id}/generate-assets")
    assert assets.status_code == 200
    assert len(assets.json()["jobs"]) == 3

    progress = await client.get(f"/api/videos/scripts/{script_id}/assets/progress")
    assert progress.status_code == 200
    assert progress.json()["states"]["queued"] == 3
