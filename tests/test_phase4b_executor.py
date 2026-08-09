"""Phase 04-B Q2 QC execution engine tests (with real ffprobe probing)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.executor import RecoverableComfyUIExecutor
from pixelle_video.media_jobs.models import Base
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.qc.checkers import ProbeError, evaluate_rule, probe_with_ffprobe
from pixelle_video.qc.executor import QCExecutor, QCJobNotFoundError
from pixelle_video.qc.repository import QCRepository

SAMPLE_VIDEO_PATH = Path(__file__).parents[1] / "02-workflows" / "sample_output.mp4"


async def _generate_video(path: Path) -> Path:
    """Generate a tiny real video (640x360, 25fps, stereo audio) via ffmpeg."""
    command = (
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=640x360:rate=25",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    )
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.communicate()
    return path


@pytest.fixture
async def media_file(tmp_path: Path):
    path = tmp_path / "out.mp4"
    await _generate_video(path)
    return path


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'exec.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = QCRepository(factory)
    job_repository = MediaJobRepository(factory)
    try:
        yield factory, repository, job_repository
    finally:
        await engine.dispose()


def _job_create() -> MediaJobCreate:
    return MediaJobCreate(
        workflow_type="a800_wan22_t2v_33f",
        workflow_key="workflow.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        node_id="a800",
        input_json={"prompt": "safe prompt"},
        input_assets_json=[],
        idempotency_key=None,
    )


# --- real ffprobe probing ----------------------------------------------------


async def test_probe_extracts_resolution_and_frame_rate(media_file: Path) -> None:
    evidence = await probe_with_ffprobe(media_file)
    assert evidence["resolution"] == "640x360"
    assert 24.0 <= evidence["frame_rate"] <= 26.0
    assert evidence["has_video"] is True


async def test_probe_extracts_audio_and_size(media_file: Path) -> None:
    evidence = await probe_with_ffprobe(media_file)
    assert evidence["has_audio"] is True
    assert evidence["audio_channels"] == 2
    assert evidence["size_bytes"] is not None and evidence["size_bytes"] > 0


async def test_probe_missing_file_raises() -> None:
    with pytest.raises(ProbeError):
        await probe_with_ffprobe("/nonexistent/definitely-missing.mp4")


# --- rule evaluation -----------------------------------------------------------


def test_evaluate_rule_eq_and_range() -> None:
    rule = {
        "id": "r1",
        "name": "测试",
        "category": "technical",
        "rule_config_json": {
            "field": "frame_rate",
            "operator": "range",
            "expected": "20-30",
            "severity": "minor",
        },
    }
    assert evaluate_rule(rule, {"frame_rate": 25.0}) is None
    issue = evaluate_rule(rule, {"frame_rate": 12.0})
    assert issue is not None and issue.severity == "minor"


def test_evaluate_rule_min_resolution_and_in() -> None:
    rule = {
        "id": "r2",
        "name": "分辨率",
        "category": "technical",
        "rule_config_json": {
            "field": "resolution",
            "operator": "min",
            "expected": "1280x720",
            "severity": "major",
        },
    }
    assert evaluate_rule(rule, {"resolution": "1920x1080"}) is None
    issue = evaluate_rule(rule, {"resolution": "640x360"})
    assert issue is not None and issue.severity == "major"
    channel_rule = {
        "id": "r3",
        "name": "声道",
        "category": "technical",
        "rule_config_json": {
            "field": "audio_channels",
            "operator": "in",
            "expected": "1,2",
            "severity": "major",
        },
    }
    assert evaluate_rule(channel_rule, {"audio_channels": 2}) is None
    assert evaluate_rule(channel_rule, {"audio_channels": 6}) is not None


def test_evaluate_rule_missing_field_is_minor_issue() -> None:
    rule = {
        "id": "r4",
        "name": "角色",
        "category": "character",
        "rule_config_json": {
            "field": "character_ref",
            "operator": "eq",
            "expected": "consistent",
            "severity": "critical",
        },
    }
    issue = evaluate_rule(rule, {})
    assert issue is not None
    assert issue.severity == "minor"
    assert "无法获取" in issue.message


# --- QCExecutor.run_qc ----------------------------------------------------------


def _default_evidence(job) -> dict:
    del job
    return {
        "character_ref": "consistent",
        "subtitle_complete": "true",
        "brand_color": "true",
        "product_info": "accurate",
        "nsfw": "false",
        "copyright_risk": "false",
        "frame_diff": "0.1",
        "brightness": "0.5",
    }


async def test_run_qc_probes_real_file_and_passes_ffprobe_rules(env, media_file: Path) -> None:
    _factory, repository, job_repository = env
    rules = [
        {
            "id": "r-res",
            "name": "分辨率检查",
            "category": "technical",
            "rule_config_json": {
                "field": "resolution",
                "operator": "min",
                "expected": "320x240",
                "severity": "major",
            },
        },
        {
            "id": "r-fps",
            "name": "帧率检查",
            "category": "technical",
            "rule_config_json": {
                "field": "frame_rate",
                "operator": "range",
                "expected": "20-30",
                "severity": "minor",
            },
        },
        {
            "id": "r-audio",
            "name": "音频声道检查",
            "category": "technical",
            "rule_config_json": {
                "field": "audio_channels",
                "operator": "in",
                "expected": "1,2",
                "severity": "major",
            },
        },
        {
            "id": "r-size",
            "name": "文件大小检查",
            "category": "technical",
            "rule_config_json": {
                "field": "size_bytes",
                "operator": "range",
                "expected": "1024-1073741824",
                "severity": "minor",
            },
        },
    ]
    for rule in rules:
        await repository.create_rule(
            name=rule["name"],
            category=rule["category"],
            rule_type="schema_validation",
            rule_config_json=rule["rule_config_json"],
            priority=1,
            provider="ffprobe",
            rule_id=rule["id"],
        )
    await repository.create_profile(
        name="default",
        rules_json=[{"rule_id": rule["id"], "severity_override": None} for rule in rules],
        is_default=1,
    )
    job = (await job_repository.create_job(_job_create())).job
    executor = QCExecutor(
        repository,
        job_lookup=job_repository.get_job,
        output_path_resolver=lambda job_id: str(media_file),
    )
    result = await executor.run_qc(job.job_id)
    assert result.profile == "default"
    assert result.total_rules == 4
    assert result.passed == 4
    assert result.issues == []


async def test_run_qc_single_rule_failure_does_not_block_others(env) -> None:
    _factory, repository, job_repository = env
    await repository.create_rule(
        name="分辨率检查",
        category="technical",
        rule_type="schema_validation",
        rule_config_json={
            "field": "resolution",
            "operator": "min",
            "expected": "1920x1080",
            "severity": "major",
        },
        priority=1,
        rule_id="r-a",
    )
    await repository.create_rule(
        name="帧率检查",
        category="technical",
        rule_type="schema_validation",
        rule_config_json={
            "field": "frame_rate",
            "operator": "range",
            "expected": "20-30",
            "severity": "minor",
        },
        priority=2,
        rule_id="r-b",
    )
    await repository.create_profile(
        name="p1", rules_json=[{"rule_id": "r-a"}, {"rule_id": "r-b"}], is_default=1
    )
    job = (await job_repository.create_job(_job_create())).job
    executor = QCExecutor(
        repository,
        job_lookup=job_repository.get_job,
        output_path_resolver=lambda job_id: None,  # no file -> probe issues
    )
    result = await executor.run_qc(job.job_id)
    assert result.total_rules == 2
    # Both rules cannot probe -> both degrade to minor issues, engine still returns.
    assert all(issue.severity == "minor" for issue in result.issues)


async def test_run_qc_unknown_profile_raises(env) -> None:
    _factory, repository, job_repository = env
    await repository.create_profile(name="p1", rules_json=[], is_default=1)
    job = (await job_repository.create_job(_job_create())).job
    executor = QCExecutor(repository, job_lookup=job_repository.get_job)
    from pixelle_video.qc.repository import QCProfileNotFoundError

    with pytest.raises(QCProfileNotFoundError):
        await executor.run_qc(job.job_id, profile_id="missing")


async def test_run_qc_missing_job_raises(env) -> None:
    _factory, repository, job_repository = env
    await repository.create_profile(name="p1", rules_json=[], is_default=1)
    executor = QCExecutor(repository, job_lookup=job_repository.get_job)
    with pytest.raises(QCJobNotFoundError):
        await executor.run_qc("no-such-job")


async def test_run_qc_profile_severity_override(env) -> None:
    _factory, repository, job_repository = env
    await repository.create_rule(
        name="分辨率检查",
        category="technical",
        rule_type="schema_validation",
        rule_config_json={
            "field": "resolution",
            "operator": "min",
            "expected": "1920x1080",
            "severity": "minor",
        },
        priority=1,
        rule_id="r-o",
    )
    await repository.create_profile(
        name="strict",
        rules_json=[{"rule_id": "r-o", "severity_override": "critical"}],
        is_default=1,
    )
    job = (await job_repository.create_job(_job_create())).job
    executor = QCExecutor(
        repository,
        job_lookup=job_repository.get_job,
        output_path_resolver=lambda job_id: None,
        evidence_provider=lambda job: {"resolution": "640x360"},
    )
    result = await executor.run_qc(job.job_id)
    assert result.total_rules == 1
    assert result.issues[0].severity == "critical"


async def test_run_qc_text_rules_use_provided_evidence(env) -> None:
    _factory, repository, job_repository = env
    await repository.create_rule(
        name="违规内容检测",
        category="platform",
        rule_type="text_analysis",
        rule_config_json={
            "field": "nsfw",
            "operator": "eq",
            "expected": "false",
            "severity": "critical",
        },
        priority=1,
        rule_id="r-n",
    )
    await repository.create_profile(name="p1", rules_json=[{"rule_id": "r-n"}], is_default=1)
    job = (await job_repository.create_job(_job_create())).job
    executor = QCExecutor(
        repository,
        job_lookup=job_repository.get_job,
        evidence_provider=lambda job: {"nsfw": "false"},
    )
    result = await executor.run_qc(job.job_id)
    assert result.passed == 1


# --- automatic trigger on the succeeded hook -----------------------------------


async def test_automatic_qc_trigger_on_succeeded_hook(env, tmp_path: Path) -> None:
    _factory, repository, job_repository = env
    job = (await job_repository.create_job(_job_create())).job
    calls: list[str] = []

    class Adapter:
        async def download_output(self, output):
            del output
            return b"\x00" * 16

    async def qc_runner(job_id: str) -> None:
        calls.append(job_id)

    executor = RecoverableComfyUIExecutor(
        job_repository,
        Adapter(),
        managed_asset_root=tmp_path / "legacy",
        managed_output_root=tmp_path / "legacy-output",
        qc_runner=qc_runner,
    )
    await executor._run_qc_diagnostic(job)
    assert calls == [job.job_id]
