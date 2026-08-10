"""Phase 06 S1 video script engine tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_video_service
from api.routers.videos import router as videos_router
from api.schemas.videos import ScriptGenerateRequest
from api.services.videos import VideoApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    job_repository = MediaJobRepository(factory)
    engine_s1 = ScriptEngine(repository)
    service = VideoApplicationService(
        repository, script_engine=engine_s1, job_repository=job_repository
    )
    try:
        yield factory, repository, service, engine_s1
    finally:
        await engine.dispose()


def _generate_body(**extra) -> dict:
    body = {
        "topic": "人工智能如何改变日常生活",
        "language": "zh-CN",
        "target_duration": 60,
        "platform": "tiktok",
    }
    body.update(extra)
    return body


async def test_engine_generates_structured_script(env) -> None:
    _factory, repository, _service, engine = env
    payload = await engine.generate_script(**_generate_body())
    script_json = payload["script_json"]
    assert script_json["hook"]
    assert len(script_json["scenes"]) == 3
    assert {scene["type"] for scene in script_json["scenes"]} == {"opening", "body", "closing"}
    assert script_json["emotion_curve"]
    assert script_json["total_duration"] == 60
    assert payload["prompt_version_id"] == "pt-short-video-script"


async def test_engine_persists_script(env) -> None:
    _factory, repository, _service, engine = env
    payload = await engine.generate_script(**_generate_body())
    script = await repository.get_script(payload["id"])
    assert script is not None and script.topic == "人工智能如何改变日常生活"
    assert script.status == "draft"


async def test_engine_scene_durations_sum_to_target(env) -> None:
    _factory, repository, _service, engine = env
    payload = await engine.generate_script(**_generate_body(target_duration=45))
    total = sum(scene["duration"] for scene in payload["script_json"]["scenes"])
    assert total == 45


async def test_engine_uses_injected_04a_compiler(env) -> None:
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, _service, engine = env
    engine.prompt_compiler = prompt_compile
    payload = await engine.generate_script(**_generate_body())
    compiled_hook = payload["script_json"]["scenes"][0]["text"]
    assert "{{topic}}" not in compiled_hook
    assert "人工智能" in compiled_hook


async def test_repository_update_and_status(env) -> None:
    _factory, repository, _service, engine = env
    payload = await engine.generate_script(**_generate_body())
    updated = await repository.update_script(
        payload["id"], script_json={"hook": "手工修改", "scenes": []}
    )
    assert updated.script_json["hook"] == "手工修改"
    confirmed = await repository.update_status(payload["id"], "confirmed")
    assert confirmed.status == "confirmed"


async def test_repository_missing_raises(env) -> None:
    _factory, repository, _service, _engine = env
    from pixelle_video.videos.repository import VideoScriptNotFoundError

    with pytest.raises(VideoScriptNotFoundError):
        await repository.update_status("missing", "confirmed")


async def test_service_confirm(env) -> None:
    _factory, repository, service, _engine = env
    payload = await service.generate_script(ScriptGenerateRequest(**_generate_body()))
    result = await service.confirm(payload["id"])
    assert result["status"] == "confirmed"


@pytest.fixture
async def api_client(env):
    _factory, _repository, service, _engine = env
    app = FastAPI()
    app.include_router(videos_router, prefix="/api")
    app.dependency_overrides[get_video_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_generate_and_get(api_client) -> None:
    client, _factory, _repository, _service = api_client
    created = await client.post("/api/videos/scripts/generate", json=_generate_body())
    assert created.status_code == 201
    body = created.json()
    assert body["topic"] == "人工智能如何改变日常生活"
    assert body["script_json"]["scenes"]

    detail = await client.get(f"/api/videos/scripts/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "draft"


async def test_api_patch_and_confirm(api_client) -> None:
    client, _factory, repository, service = api_client
    payload = await service.generate_script(ScriptGenerateRequest(**_generate_body()))
    patched = await client.patch(
        f"/api/videos/scripts/{payload['id']}",
        json={"status": "confirmed"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "confirmed"

    confirmed = await client.post(f"/api/videos/scripts/{payload['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
