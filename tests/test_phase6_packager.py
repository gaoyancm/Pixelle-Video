"""Phase 06 S4 multi-platform packaging tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_video_service
from api.routers.videos import router as videos_router
from api.services.videos import VideoApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.videos.packager import VIDEO_PLATFORM_SPECS, VideoPackager
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's4.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    exports_root = tmp_path / "exports"
    packager = VideoPackager(
        repository, exports_root=str(exports_root), require_video=False
    )
    service = VideoApplicationService(
        repository, script_engine=ScriptEngine(repository), packager=packager
    )
    try:
        yield factory, repository, service, exports_root
    finally:
        await engine.dispose()


async def _make_script(repository: VideoScriptRepository, project_id: str = "project-x") -> str:
    payload = await ScriptEngine(repository).generate_script(
        topic="人工智能如何改变日常生活",
        language="zh-CN",
        target_duration=60,
        project_id=project_id,
    )
    return payload["id"]


def test_specs_cover_four_platforms() -> None:
    assert set(VIDEO_PLATFORM_SPECS) == {"tiktok", "reels", "youtube_shorts", "generic_landscape"}
    assert VIDEO_PLATFORM_SPECS["tiktok"]["size"] == (1080, 1920)
    assert VIDEO_PLATFORM_SPECS["youtube_shorts"]["size"] == (1920, 1080)


async def test_package_creates_platform_directories(env) -> None:
    _factory, repository, service, exports_root = env
    script_id = await _make_script(repository)
    payload = await service.package(script_id, ["tiktok", "youtube_shorts"])
    assert set(payload["platforms"]) == {"tiktok", "youtube_shorts"}
    for platform in ("tiktok", "youtube_shorts"):
        platform_dir = exports_root / "project-x" / script_id / "video_delivery" / platform
        assert platform_dir.is_dir()


async def test_package_writes_titles_and_descriptions(env) -> None:
    _factory, repository, service, exports_root = env
    script_id = await _make_script(repository)
    await service.package(script_id, ["tiktok"])
    platform_dir = exports_root / "project-x" / script_id / "video_delivery" / "tiktok"
    titles = json.loads((platform_dir / "titles.json").read_text(encoding="utf-8"))
    descriptions = json.loads((platform_dir / "descriptions.json").read_text(encoding="utf-8"))
    assert "zh-CN" in titles and "en-US" in titles
    assert "人工智能如何改变日常生活" in titles["zh-CN"]
    assert "zh-CN" in descriptions


async def test_package_cover_is_real_image(env) -> None:
    _factory, repository, service, exports_root = env
    script_id = await _make_script(repository)
    await service.package(script_id, ["reels"])
    platform_dir = exports_root / "project-x" / script_id / "video_delivery" / "reels"
    cover = platform_dir / "cover_1080x1920.jpg"
    assert cover.exists()
    assert cover.stat().st_size > 0
    # The cover must be a decodable image of the exact platform size.
    from PIL import Image

    with Image.open(cover) as image:
        assert image.format == "JPEG"
        assert image.size == (1080, 1920)


async def test_package_languages_include_secondary(env) -> None:
    _factory, repository, service, exports_root = env
    script_id = await _make_script(repository)
    payload = await service.package(script_id, ["tiktok"])
    assert "en-US" in payload["platforms"]["tiktok"]["languages"]


async def test_zip_package_creates_archive(env) -> None:
    _factory, repository, service, exports_root = env
    script_id = await _make_script(repository)
    await service.package(script_id, ["tiktok", "reels"])
    zip_path = service.download_zip(script_id, "project-x")
    assert zip_path is not None
    assert Path(zip_path).exists()
    import zipfile

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert any("tiktok/titles.json" in name for name in names)
        assert any("reels/descriptions.json" in name for name in names)


@pytest.fixture
async def api_client(env):
    _factory, _repository, service, _exports = env
    app = FastAPI()
    app.include_router(videos_router, prefix="/api")
    app.dependency_overrides[get_video_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_package_and_download(api_client) -> None:
    client, _factory, repository, service = api_client
    script_id = await _make_script(repository)
    packaged = await client.post(
        f"/api/videos/scripts/{script_id}/package",
        params={"platforms": ["tiktok", "youtube_shorts"]},
    )
    assert packaged.status_code == 200
    body = packaged.json()
    assert "tiktok" in body["platforms"]
    assert body["topic"] == "人工智能如何改变日常生活"

    # Build the zip first through the service then download.
    service.download_zip(script_id, "project-x")
    downloaded = await client.get(f"/api/videos/scripts/{script_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"


async def test_package_adapts_composed_video_per_platform(env, tmp_path: Path) -> None:
    """D2: packaging transcodes the composed video into the platform spec
    (size + duration) and the zip contains the .mp4 files."""
    import shutil
    import subprocess

    from pixelle_video.videos.compose import Composer

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    _factory, repository, service, exports_root = env
    script_id = await _make_script(repository)

    # Compose a small real video (placeholder frames via ffmpeg).
    async def fake_tts(text: str) -> str:
        audio = tmp_path / "voice.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono:d=3",
                "-c:a",
                "libmp3lame",
                str(audio),
            ],
            check=True,
            capture_output=True,
        )
        return str(audio)

    composer = Composer(repository, tts_runner=fake_tts, work_dir=str(tmp_path / "work"))
    service.composer = composer  # wire so package() resolves the composed video
    compose_payload = await composer.compose(script_id)
    assert Path(compose_payload["video"]).exists()

    # Package with the composed video path.
    await service.package(script_id, ["tiktok", "youtube_shorts"])
    for platform, spec in (
        ("tiktok", VIDEO_PLATFORM_SPECS["tiktok"]),
        ("youtube_shorts", VIDEO_PLATFORM_SPECS["youtube_shorts"]),
    ):
        platform_dir = exports_root / "project-x" / script_id / "video_delivery" / platform
        mp4s = list(platform_dir.glob("*.mp4"))
        assert len(mp4s) == 1, f"platform {platform} missing adapted mp4"
        width, height, duration = _probe(mp4s[0])
        assert (width, height) == tuple(spec["size"])
        assert duration <= spec["max_seconds"] + 0.5 if spec["max_seconds"] else True

    # Zip contains the mp4 files.
    zip_path = service.download_zip(script_id, "project-x")
    assert zip_path is not None
    import zipfile

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert any("tiktok/" in name and name.endswith(".mp4") for name in names)
        assert any("youtube_shorts/" in name and name.endswith(".mp4") for name in names)


def _probe(path: Path) -> tuple[int, int, float]:
    import json
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"]), float(data["format"]["duration"])
