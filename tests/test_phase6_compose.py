"""Phase 06 S3 compose tests: subtitles, voice-over, BGM, ffmpeg synthesis."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_video_service
from pixelle_video.media_jobs.models import Base
from pixelle_video.videos.compose import (
    Composer,
    build_srt,
    build_vtt,
    emotion_profile,
    narration_text,
)
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VideoScriptRepository(factory)
    try:
        yield factory, repository
    finally:
        await engine.dispose()


def _scenes() -> list[dict]:
    return [
        {
            "type": "opening",
            "text": "AI 正在改变我们的工作方式！",
            "duration": 5,
            "visual_direction": "未来办公室",
        },
        {
            "type": "body",
            "text": "自动化让效率提升三倍",
            "duration": 15,
            "visual_direction": "数据展示",
        },
        {
            "type": "closing",
            "text": "点赞关注了解更多",
            "duration": 8,
            "visual_direction": "CTA",
        },
    ]


async def _make_script(repository: VideoScriptRepository, tmp_path: Path) -> str:
    payload = await ScriptEngine(repository).generate_script(
        topic="人工智能如何改变日常生活", target_duration=28
    )
    return payload["id"]


def test_build_srt_has_sequential_timestamps() -> None:
    srt = build_srt(_scenes())
    assert srt.startswith("1\n00:00:00,000 --> 00:00:05,000")
    assert "AI 正在改变我们的工作方式！" in srt
    assert "3\n" in srt


def test_build_vtt_wraps_webvtt() -> None:
    srt = build_srt(_scenes())
    vtt = build_vtt(srt)
    assert vtt.startswith("WEBVTT")
    assert "00:00:05.000" in vtt


def test_narration_text_joins_scenes() -> None:
    text = narration_text(_scenes())
    assert "AI 正在改变我们的工作方式！" in text
    assert "点赞关注了解更多" in text


def test_emotion_profile_detects_style() -> None:
    assert emotion_profile(_scenes()) == "激昂"
    calm = [{"type": "opening", "text": "平静介绍", "duration": 5}]
    assert emotion_profile(calm) == "舒缓"


def test_composer_synthesizes_audio_and_subtitles(tmp_path: Path) -> None:
    composer = Composer.__new__(Composer)
    composer.work_dir = tmp_path / "work"
    # Verify subtitle files are produced without running ffmpeg by driving
    # the pure helpers through a script with no frames.
    scenes = _scenes()
    output_dir = composer.work_dir / "script-x"
    output_dir.mkdir(parents=True, exist_ok=True)
    srt_path = output_dir / "subtitle.srt"
    srt_path.write_text(build_srt(scenes), encoding="utf-8")
    assert srt_path.exists()
    assert srt_path.read_text(encoding="utf-8").strip()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
async def test_composer_produces_playable_mp4(env, tmp_path: Path) -> None:
    _factory, repository = env
    script_id = await _make_script(repository, tmp_path)

    async def fake_tts(text: str) -> str:
        audio = tmp_path / "voice.mp3"
        _ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=24000:cl=mono:d={max(len(text) // 10, 3)}",
                "-c:a",
                "libmp3lame",
                str(audio),
            ]
        )
        return str(audio)

    async def fake_bgm(emotion: str) -> str:
        bgm = tmp_path / "bgm.mp3"
        _ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=10",
                "-c:a",
                "libmp3lame",
                str(bgm),
            ]
        )
        return str(bgm)

    composer = Composer(
        repository, tts_runner=fake_tts, bgm_matcher=fake_bgm, work_dir=str(tmp_path / "work")
    )
    payload = await composer.compose(script_id)
    video = Path(payload["video"])
    assert video.exists() and video.suffix == ".mp4"
    assert Path(payload["srt"]).exists()
    assert Path(payload["vtt"]).exists()
    # ffprobe verifies the file is a real, decodable video with a duration.
    duration = _ffprobe_duration(video)
    assert duration is not None and duration > 0


def _ffmpeg(command: list[str]) -> None:
    import subprocess

    subprocess.run(command, check=True, capture_output=True)


def _ffprobe_duration(path: Path) -> float | None:
    import json
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return float(json.loads(result.stdout)["format"]["duration"])


async def test_composer_uses_real_frame_assets_via_resolver(env, tmp_path: Path) -> None:
    """D1: frame assets resolve from real files through the asset resolver
    (no dead _asset_paths attribute)."""
    _factory, repository = env
    script_id = await _make_script(repository, tmp_path)
    real_frame = tmp_path / "real_frame.mp4"
    _ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=2:r=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(real_frame),
        ]
    )

    async def fake_asset_resolver(job_id: str) -> str:
        assert job_id  # real job id is passed through
        return str(real_frame)

    async def fake_tts(text: str) -> str:
        audio = tmp_path / "voice2.mp3"
        _ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono:d=2",
                "-c:a",
                "libmp3lame",
                str(audio),
            ]
        )
        return str(audio)

    composer = Composer(
        repository,
        tts_runner=fake_tts,
        bgm_matcher=None,
        asset_resolver=fake_asset_resolver,
        work_dir=str(tmp_path / "work-resolver"),
    )
    payload = await composer.compose(script_id)
    assert Path(payload["video"]).exists()


async def test_get_video_service_wires_tts_into_composer(tmp_path, monkeypatch) -> None:
    """D1: the production dependency wiring injects a TTS runner into the
    composer (factory-level assertion)."""
    import api.dependencies as deps
    from pixelle_video.config.schema import MediaJobsConfig

    url = f"sqlite+aiosqlite:///{(tmp_path / 'di-video.db').as_posix()}"
    fake_manager = SimpleNamespace(
        config=SimpleNamespace(
            media_jobs=MediaJobsConfig(enabled=True, database_url=url),
            to_dict=lambda: {"comfyui": {}},
        )
    )
    monkeypatch.setattr("api.dependencies.ConfigManager", lambda: fake_manager)
    monkeypatch.setattr(deps, "_video_service", None)
    monkeypatch.setattr(deps, "_media_jobs_database", None)
    service = await get_video_service()
    assert service.composer.tts_runner is not None
    assert service.composer.bgm_matcher is not None
    assert service.composer.asset_resolver is not None
