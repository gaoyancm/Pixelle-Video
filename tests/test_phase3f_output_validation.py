"""Phase 03-F F3 output contract validation tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pixelle_video.audit import AuditRepository
from pixelle_video.media_assets.models import MediaAsset, MediaJobAsset, OutputSchema
from pixelle_video.media_assets.repository import AssetRepository
from pixelle_video.media_assets.service import AssetService
from pixelle_video.media_assets.store import LocalAssetStore
from pixelle_video.media_assets.validation import validate_outputs
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.models import Base, MediaJob
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus

T2V_SCHEMA = {
    "workflow_type": "a800_wan22_t2v_33f",
    "expected_outputs": [
        {
            "media_type": "video",
            "mime_type_pattern": "^video/",
            "min_size_bytes": 1024,
            "max_size_bytes": 1_073_741_824,
            "min_duration": 0.5,
            "max_duration": 30.0,
            "min_width": 128,
            "min_height": 128,
        }
    ],
}

GOOD_OUTPUT = [
    {
        "exists": True,
        "mime_type": "video/mp4",
        "size_bytes": 2048,
        "duration": 3.0,
        "width": 640,
        "height": 360,
    }
]


# --- Pure validation function ------------------------------------------------


def test_validation_passes_when_outputs_match_schema() -> None:
    result = validate_outputs(schema=T2V_SCHEMA, outputs=GOOD_OUTPUT)
    assert result.passed is True
    assert result.issues == ()


def test_validation_reports_missing_required_output_as_critical() -> None:
    result = validate_outputs(schema=T2V_SCHEMA, outputs=[])
    assert result.passed is False
    assert any(issue.severity == "critical" for issue in result.issues)
    assert result.issues[0].field == "outputs[0].exists"


def test_validation_reports_mime_mismatch_as_major() -> None:
    outputs = [
        {
            "exists": True,
            "mime_type": "image/png",
            "size_bytes": 2048,
            "duration": 3.0,
            "width": 640,
            "height": 360,
        }
    ]
    result = validate_outputs(schema=T2V_SCHEMA, outputs=outputs)
    assert result.passed is True  # only major issue -> not critical
    assert any(
        issue.severity == "major" and issue.field == "outputs[0].mime_type"
        for issue in result.issues
    )


def test_validation_reports_size_out_of_range_as_major() -> None:
    outputs = [
        {
            "exists": True,
            "mime_type": "video/mp4",
            "size_bytes": 1,
            "duration": 3.0,
            "width": 640,
            "height": 360,
        }
    ]
    result = validate_outputs(schema=T2V_SCHEMA, outputs=outputs)
    assert any(
        issue.severity == "major" and issue.field == "outputs[0].size_bytes"
        for issue in result.issues
    )


def test_validation_reports_duration_and_resolution_issues() -> None:
    outputs = [
        {
            "exists": True,
            "mime_type": "video/mp4",
            "size_bytes": 2048,
            "duration": 60.0,
            "width": 64,
            "height": 360,
        }
    ]
    result = validate_outputs(schema=T2V_SCHEMA, outputs=outputs)
    fields = {issue.field for issue in result.issues}
    assert "outputs[0].duration" in fields
    assert "outputs[0].resolution" in fields


def test_validation_missing_output_entry_is_critical() -> None:
    outputs = [{"exists": True, "mime_type": "video/mp4"}]
    schema = {
        "expected_outputs": [
            {"media_type": "video"},
            {"media_type": "video"},
        ]
    }
    result = validate_outputs(schema=schema, outputs=outputs)
    assert result.passed is False
    assert any(
        issue.field == "outputs[1].exists" and issue.severity == "critical"
        for issue in result.issues
    )


# --- AssetService integration ------------------------------------------------


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


@pytest.fixture
async def asset_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'f3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalAssetStore(tmp_path / "store")
    service = AssetService(AssetRepository(factory), store, max_upload_size=10 * 1024 * 1024)
    repository = MediaJobRepository(factory)
    audit = AuditRepository(factory)
    try:
        yield factory, service, repository, audit, store
    finally:
        await engine.dispose()


async def _commit_output_job(asset_env, *, mime_type: str, size: int) -> str:
    factory, service, repository, _audit, store = asset_env
    job = (await repository.create_job(_job_create())).job
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        expected_version=job.version,
        target_status=JobStatus.SUBMITTING,
    )
    job = await repository.transition_status(
        job.job_id,
        expected_status=JobStatus.SUBMITTING,
        expected_version=job.version,
        target_status=JobStatus.RUNNING,
    )
    object_key = f"outputs/{job.job_id}.mp4"
    store.write_stream(io.BytesIO(b"x" * size), object_key=object_key, max_bytes=10 * 1024 * 1024)
    asset = MediaAsset(
        id=f"asset-{job.job_id[:8]}",
        kind="output",
        state="available",
        backend="local",
        object_key=object_key,
        original_filename="out.mp4",
        media_type="video",
        mime_type=mime_type,
        size_bytes=size,
        sha256="a" * 64,
        source="generated",
    )
    async with factory() as session:
        async with session.begin():
            session.add(asset)
            await session.flush()
            session.add(
                MediaJobAsset(
                    job_id=job.job_id,
                    asset_id=asset.id,
                    direction="output",
                    role="generated_video",
                    position=0,
                )
            )
    async with factory() as session:
        async with session.begin():
            job = await session.get(MediaJob, job.job_id)
            job.output_metadata = [
                {
                    "output_id": asset.id,
                    "media_type": "video",
                    "relative_path": object_key,
                    "size": size,
                    "mime_type": mime_type,
                    "sha256": "a" * 64,
                    "duration": 3.0,
                    "width": 640,
                    "height": 360,
                }
            ]
    return job.job_id


async def test_validate_output_contract_passes_for_matching_output(asset_env) -> None:
    factory, service, repository, audit, store = asset_env
    job_id = await _commit_output_job(asset_env, mime_type="video/mp4", size=2048)
    result = await service.validate_output_contract(job_id, T2V_SCHEMA)
    assert result.passed is True
    assert result.issues == ()


async def test_validate_output_contract_detects_missing_file(asset_env) -> None:
    factory, service, repository, audit, store = asset_env
    job_id = await _commit_output_job(asset_env, mime_type="video/mp4", size=2048)
    (store.root / "outputs" / f"{job_id}.mp4").unlink()
    result = await service.validate_output_contract(job_id, T2V_SCHEMA)
    assert result.passed is False
    assert result.issues[0].field == "outputs[0].exists"


async def test_validate_and_record_writes_audit_events(asset_env) -> None:
    factory, service, repository, audit, store = asset_env
    job_id = await _commit_output_job(asset_env, mime_type="image/png", size=1)
    async with factory() as session:
        schema = OutputSchema(
            schema_id="schema-t2v-33f",
            workflow_type="a800_wan22_t2v_33f",
            schema_json=T2V_SCHEMA,
        )
        session.add(schema)
        await session.commit()
    result = await service.validate_and_record_output_contract(job_id, audit=audit)
    assert result.issues  # major issues recorded but task completion is not blocked
    events, _ = await audit.list(scope_type="job", scope_id=job_id)
    assert any(event.event_type == "output_validated" for event in events)
    validated = next(event for event in events if event.event_type == "output_validated")
    assert validated.details_json["workflow_type"] == "a800_wan22_t2v_33f"
    assert validated.details_json["issues"]


async def test_validate_and_record_without_schema_is_noop(asset_env) -> None:
    factory, service, repository, audit, store = asset_env
    job_id = await _commit_output_job(asset_env, mime_type="video/mp4", size=2048)
    result = await service.validate_and_record_output_contract(job_id, audit=audit)
    assert result.passed is True
    events, _ = await audit.list(scope_type="job", scope_id=job_id)
    assert not any(event.event_type == "output_validated" for event in events)
