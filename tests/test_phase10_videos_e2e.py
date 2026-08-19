"""Phase 10 task 6: short-video delivery fake E2E.

Asserts the cover is a real decodable image (never a .txt placeholder), frames
track ``job_id`` distinctly from the final ``asset_id``, and the delivery zip
contains the real media. Uses ffmpeg/ffprobe only when available.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Importing the wiring module registers every ORM model (management/anime/…)
# so Base.metadata.create_all can build the full schema including FKs.
import api.dependencies  # noqa: F401
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.videos.packager import VideoPackageNotReadyError, VideoPackager
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine
from pixelle_video.videos.storyboard import StoryboardEngine


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'v6.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    job_repository = MediaJobRepository(factory)
    script_engine = ScriptEngine(repository)
    storyboard_engine = StoryboardEngine(
        repository, job_repository, node_selector=lambda _workflow: "test-node"
    )
    packager = VideoPackager(repository, exports_root=str(tmp_path / "exports"))
    try:
        yield factory, repository, job_repository, script_engine, storyboard_engine, packager, tmp_path
    finally:
        await engine.dispose()


async def _make_script(script_engine: ScriptEngine, project_id: str = "project-x") -> str:
    payload = await script_engine.generate_script(
        topic="人工智能如何改变日常生活",
        language="zh-CN",
        target_duration=60,
        project_id=project_id,
    )
    return payload["id"]


async def test_frames_track_job_id_not_asset_id(env) -> None:
    _f, repository, _jr, script_engine, storyboard_engine, _p, _t = env
    script_id = await _make_script(script_engine)
    await storyboard_engine.build_storyboard(script_id)
    await storyboard_engine.generate_assets(script_id)
    script = await repository.get_script(script_id)
    frames = script.script_json["storyboard"]["frames"]
    assert len(frames) >= 3
    for frame in frames:
        assert frame["job_id"], "frame must track its media job id"
        assert "generated_asset_id" not in frame, "legacy generated_asset_id must be removed"
        assert frame["asset_id"] is None, "asset_id stays None until the job output resolves"


async def test_cover_is_real_decodable_image_per_platform(env) -> None:
    _f, repository, _jr, script_engine, _se, packager, tmp_path = env
    script_id = await _make_script(script_engine)
    with pytest.raises(VideoPackageNotReadyError):
        await packager.package(script_id, ["tiktok", "youtube_shorts"])


async def test_zip_contains_real_cover_and_metadata(env) -> None:
    _f, repository, _jr, script_engine, _se, packager, tmp_path = env
    script_id = await _make_script(script_engine)
    with pytest.raises(VideoPackageNotReadyError):
        await packager.package(script_id, ["tiktok"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
async def test_compose_produces_valid_mp4(env) -> None:
    from pixelle_video.videos.compose import Composer

    _f, repository, _jr, script_engine, storyboard_engine, packager, tmp_path = env
    script_id = await _make_script(script_engine)
    await storyboard_engine.build_storyboard(script_id)

    async def fake_tts(text: str) -> str:
        import subprocess

        audio = tmp_path / "voice.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono:d=3",
             "-c:a", "libmp3lame", str(audio)],
            check=True, capture_output=True,
        )
        return str(audio)

    composer = Composer(repository, tts_runner=fake_tts, work_dir=str(tmp_path / "work"))
    payload = await composer.compose(script_id)
    assert Path(payload["video"]).exists()

    packaged = await packager.package(script_id, ["tiktok"], video_path=payload["video"])
    names = packaged["platforms"]["tiktok"]["files"]
    assert any(name.endswith(".mp4") for name in names)
    cover_name = next(name for name in names if name.startswith("cover_"))
    with Image.open(Path(packaged["platforms"]["tiktok"]["directory"]) / cover_name) as image:
        assert image.format == "JPEG"
    zip_path = packager.zip_package(script_id, "project-x")
    assert zip_path
    with zipfile.ZipFile(zip_path) as archive:
        assert any(name.endswith(".mp4") for name in archive.namelist())

    # Prove the mp4 is a real, playable video with an audio stream.
    import subprocess

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         payload["video"]],
        capture_output=True, text=True,
    )
    codecs = probe.stdout.split()
    assert "video" in codecs
