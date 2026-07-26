from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pixelle_video.media_assets import (
    AssetIdempotencyConflictError,
    AssetKind,
    AssetReferencedError,
    AssetRepository,
    AssetService,
    AssetState,
    AssetUnavailableError,
    LocalAssetStore,
    ObjectTooLargeError,
    StoreBoundaryError,
    UnsupportedMediaError,
)
from pixelle_video.media_assets.contracts import validate_object_key
from pixelle_video.media_assets.models import MediaAsset, MediaJobAsset
from pixelle_video.media_assets.repository import (
    OutputRegistrationDisposition,
    OutputRegistrationError,
)
from pixelle_video.media_jobs.contracts import (
    MediaInputAsset,
    MediaJobCreate,
    MediaOutputMetadata,
)
from pixelle_video.media_jobs.executor import RecoverableComfyUIExecutor
from pixelle_video.media_jobs.models import Base, utc_now
from pixelle_video.media_jobs.repository import CASConflictError, MediaJobRepository
from pixelle_video.media_jobs.state_machine import JobStatus
from pixelle_video.media_jobs.worker import LeaseLostError
from pixelle_video.media_jobs.worker_cli import _build_executor
from pixelle_video.services.comfyui_adapter import ComfyUIOutput

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


@pytest.fixture
async def asset_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'assets.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AssetRepository(factory)
    store = LocalAssetStore(tmp_path / "store")
    service = AssetService(repository, store, max_upload_size=1024)
    try:
        yield service, factory
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../escape",
        "objects/../escape",
        "/absolute",
        r"C:\escape",
        "C:escape",
        r"objects\escape",
        "objects//escape",
        "objects/\x00escape",
    ],
)
def test_object_key_rejects_path_forms(key):
    with pytest.raises(ValueError):
        validate_object_key(key)


def test_local_store_streams_hashes_and_enforces_limit(tmp_path):
    store = LocalAssetStore(tmp_path / "store")
    result = store.write_stream(
        io.BytesIO(PNG), object_key="objects/aa/value.png", max_bytes=1024, chunk_size=7
    )
    assert result.size_bytes == len(PNG)
    assert len(result.sha256) == 64
    assert store.open(result.object_key).read() == PNG
    with pytest.raises(ObjectTooLargeError):
        store.write_stream(
            io.BytesIO(PNG), object_key="objects/aa/too-large.png", max_bytes=5
        )
    assert not (tmp_path / "store" / "objects" / "aa" / "too-large.png").exists()


def test_local_store_rejects_symlink_escape(tmp_path):
    store = LocalAssetStore(tmp_path / "store")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "store" / "objects"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        assert not link.exists()
        return
    with pytest.raises(StoreBoundaryError):
        store.write_stream(
            io.BytesIO(PNG), object_key="objects/aa/value.png", max_bytes=1024
        )


@pytest.mark.asyncio
async def test_upload_is_uuid_registered_and_idempotent(asset_context):
    service, _ = asset_context
    first, created = await service.upload(
        io.BytesIO(PNG),
        filename="../../client.png",
        mime_type="image/png",
        idempotency_key="upload-1",
    )
    assert created
    assert first.kind == AssetKind.INPUT.value
    assert first.state == AssetState.AVAILABLE.value
    assert first.original_filename == "client.png"
    assert first.id != first.object_key
    assert Path(first.object_key).name.startswith(first.id)

    replay, created = await service.upload(
        io.BytesIO(PNG),
        filename="../../client.png",
        mime_type="image/png",
        idempotency_key="upload-1",
    )
    assert not created
    assert replay.id == first.id
    assert list(service.store.iter_object_keys()) == [first.object_key]


@pytest.mark.asyncio
async def test_upload_idempotency_conflict_compensates_file(asset_context):
    service, _ = asset_context
    await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="upload-1",
    )
    with pytest.raises(AssetIdempotencyConflictError):
        await service.upload(
            io.BytesIO(PNG + b"different"),
            filename="client.png",
            mime_type="image/png",
            idempotency_key="upload-1",
        )
    assert len(list(service.store.iter_object_keys())) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_upload_has_one_asset_and_object(asset_context):
    service, _ = asset_context

    async def upload():
        return await service.upload(
            io.BytesIO(PNG),
            filename="client.png",
            mime_type="image/png",
            idempotency_key="concurrent-upload",
        )

    first, second = await asyncio.gather(upload(), upload())
    assert first[0].id == second[0].id
    assert sorted([first[1], second[1]]) == [False, True]
    assert list(service.store.iter_object_keys()) == [first[0].object_key]


@pytest.mark.asyncio
async def test_upload_rejects_empty_mismatch_and_unsupported(asset_context):
    service, _ = asset_context
    with pytest.raises(ValueError):
        await service.upload(
            io.BytesIO(b""),
            filename="empty.png",
            mime_type="image/png",
            idempotency_key="empty",
        )
    with pytest.raises(UnsupportedMediaError):
        await service.upload(
            io.BytesIO(b"not png"),
            filename="fake.png",
            mime_type="image/png",
            idempotency_key="fake",
        )
    with pytest.raises(UnsupportedMediaError):
        await service.upload(
            io.BytesIO(PNG),
            filename="wrong.jpg",
            mime_type="image/png",
            idempotency_key="wrong",
        )
    assert list(service.store.iter_object_keys()) == []


@pytest.mark.asyncio
async def test_soft_delete_is_idempotent_and_blocks_content(asset_context):
    service, _ = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="upload-1",
    )
    first = await service.soft_delete(asset.id)
    second = await service.soft_delete(asset.id)
    assert first.state == second.state == AssetState.DELETED.value
    assert first.deleted_at == second.deleted_at
    with pytest.raises(AssetUnavailableError):
        await service.open_content(asset.id)


@pytest.mark.asyncio
async def test_physical_cleanup_rejects_referenced_asset(asset_context):
    service, factory = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="upload-1",
    )
    from pixelle_video.media_jobs.models import MediaJob

    async with factory() as session:
        async with session.begin():
            session.add(
                MediaJob(
                    job_id="00000000-0000-4000-8000-000000000001",
                    workflow_type="i2v",
                    workflow_key="workflow.json",
                    executor_kind="private_comfyui",
                    provider="private_comfyui",
                    status="queued",
                    input_json={},
                    input_assets_json=[{"asset_id": asset.id}],
                    submission_token="token",
                    request_hash="a" * 64,
                    output_metadata=[],
                    retry_count=0,
                    version=1,
                    remote_status="unknown",
                    remote_termination_status="unknown",
                )
            )
            session.add(
                MediaJobAsset(
                    job_id="00000000-0000-4000-8000-000000000001",
                    asset_id=asset.id,
                    direction="input",
                    role="input_image",
                    position=0,
                )
            )
    with pytest.raises(AssetReferencedError):
        await service.physical_cleanup(asset.id)


@pytest.mark.asyncio
async def test_reconciliation_reports_missing_orphan_and_succeeded_gap(asset_context):
    service, factory = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="upload-1",
    )
    service.store.delete(asset.object_key)
    service.store.write_stream(
        io.BytesIO(PNG), object_key="objects/ff/orphan.png", max_bytes=1024
    )
    from pixelle_video.media_jobs.models import MediaJob

    async with factory() as session:
        async with session.begin():
            session.add(
                MediaJob(
                    job_id="00000000-0000-4000-8000-000000000002",
                    workflow_type="t2v",
                    workflow_key="workflow.json",
                    executor_kind="private_comfyui",
                    provider="private_comfyui",
                    status="succeeded",
                    input_json={},
                    input_assets_json=[],
                    submission_token="token",
                    request_hash="b" * 64,
                    output_metadata=[],
                    retry_count=0,
                    version=1,
                    remote_status="completed",
                    remote_termination_status="unknown",
                )
            )
    report = await service.reconcile()
    assert report.missing_assets == (asset.id,)
    assert report.orphan_objects == ("objects/ff/orphan.png",)
    assert report.succeeded_jobs_without_outputs == (
        "00000000-0000-4000-8000-000000000002",
    )


@pytest.mark.asyncio
async def test_output_group_registration_is_owned_atomic_and_uuid_based(asset_context):
    service, factory = asset_context
    input_asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="input-output-test",
    )
    jobs = MediaJobRepository(factory)
    created = await jobs.create_job_with_assets(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "safe"},
            input_assets_json=[
                MediaInputAsset(asset_id=input_asset.id, role="input_image")
            ],
        )
    )
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(type(created.job))
                .where(type(created.job).job_id == created.job.job_id)
                .values(status="running", lease_owner="worker-1")
            )
    running = await jobs.get_job(created.job.job_id)
    assert running is not None
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"x" * 16
    output_asset = await service.register_generated_bytes(mp4, filename="result.mp4")
    metadata = MediaOutputMetadata(
        output_id=output_asset.id,
        media_type="video",
        relative_path=output_asset.object_key,
        size=output_asset.size_bytes,
        mime_type=output_asset.mime_type,
        sha256=output_asset.sha256,
    )
    updated = await service.repository.register_output_group(
        job_id=running.job_id,
        lease_owner="worker-1",
        expected_version=running.version,
        assets=[output_asset],
        roles=["generated_video"],
        output_metadata=[metadata.model_dump()],
    )
    assert updated.output_metadata[0]["output_id"] == output_asset.id
    async with factory() as session:
        relations = list(
            (
                await session.execute(
                    select(MediaJobAsset)
                    .where(MediaJobAsset.job_id == running.job_id)
                    .order_by(MediaJobAsset.direction, MediaJobAsset.position)
                )
            ).scalars()
        )
    assert [(item.direction, item.asset_id) for item in relations] == [
        ("input", input_asset.id),
        ("output", output_asset.id),
    ]
    with pytest.raises(CASConflictError):
        await service.repository.register_output_group(
            job_id=running.job_id,
            lease_owner="worker-1",
            expected_version=running.version,
            assets=[output_asset],
            roles=["generated_video"],
            output_metadata=[metadata.model_dump()],
        )


@pytest.mark.asyncio
async def test_executor_resolves_uuid_input_and_stages_uuid_outputs(asset_context, tmp_path):
    service, factory = asset_context
    input_asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="executor-input",
    )
    jobs = MediaJobRepository(factory)
    created = await jobs.create_job_with_assets(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "safe"},
            input_assets_json=[MediaInputAsset(asset_id=input_asset.id)],
        )
    )

    class Adapter:
        async def download_output(self, output):
            del output
            return b"\x00\x00\x00\x18ftypisom" + b"x" * 16

    executor = RecoverableComfyUIExecutor(
        jobs,
        Adapter(),
        managed_asset_root=tmp_path / "legacy",
        managed_output_root=tmp_path / "legacy-output",
        asset_service=service,
    )
    parameters = await executor._build_parameters(created.job)
    assert parameters["image_path"] == service.store.local_path(input_asset.object_key)
    metadata, assets = await executor._store_outputs(
        created.job,
        [
            ComfyUIOutput(
                node_id="1",
                filename="result.mp4",
                subfolder="",
                storage_type="output",
                media_type="video",
                url="/view",
            )
        ],
    )
    assert len(metadata) == len(assets) == 1
    assert metadata[0].output_id == assets[0].id
    assert metadata[0].output_id != metadata[0].relative_path
    service.discard_unregistered(assets[0])


def _production_manager(tmp_path):
    media_jobs = SimpleNamespace(
        config_base_dir=str(tmp_path),
        asset_store_root="store",
        asset_max_upload_size=1024,
        managed_asset_root="legacy",
        managed_output_root="legacy-output",
        history_poll_interval_seconds=0.01,
    )
    return SimpleNamespace(
        config=SimpleNamespace(
            media_jobs=media_jobs,
            comfyui=SimpleNamespace(nodes=[]),
        )
    )


@pytest.mark.asyncio
async def test_production_worker_assembly_routes_relations_and_legacy(asset_context, tmp_path):
    _service, factory = asset_context
    executor = _build_executor(_production_manager(tmp_path), factory)
    input_asset, _ = await executor.asset_service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="production-input",
    )
    created = await executor.repository.create_job_with_assets(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "new"},
            input_assets_json=[MediaInputAsset(asset_id=input_asset.id)],
        )
    )
    assert (await executor._build_parameters(created.job))["image_path"] == (
        executor.asset_service.store.local_path(input_asset.object_key)
    )

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_file = legacy_root / "historic.png"
    legacy_file.write_bytes(PNG)
    legacy = await executor.repository.create_job(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "legacy"},
            input_assets_json=[MediaInputAsset(asset_id="historic.png")],
        )
    )
    assert (await executor._build_parameters(legacy.job))["image_path"] == legacy_file
    assert await executor.asset_service.repository.input_assets_for_job(legacy.job.job_id) == []
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(type(legacy.job))
                .where(type(legacy.job).job_id == legacy.job.job_id)
                .values(status="failed", error_category="remote_failed")
            )
    retried = await executor.repository.create_retry_job(
        legacy.job.job_id,
        idempotency_key="legacy-retry",
        deadline_at=utc_now() + timedelta(hours=1),
    )
    assert (await executor._build_parameters(retried.job))["image_path"] == legacy_file
    assert await executor.asset_service.repository.input_assets_for_job(retried.job.job_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "00000000-0000-4000-8000-000000000000",
        "not-a-uuid-0000-4000-8000-000000000000",
        "../escape.png",
        "/absolute.png",
        r"C:\escape.png",
        "C:escape.png",
        r"\\server\share.png",
        r"mixed/path\escape.png",
    ],
)
async def test_production_legacy_fallback_rejects_uuid_and_dangerous_values(
    asset_context, tmp_path, unsafe
):
    _service, factory = asset_context
    executor = _build_executor(_production_manager(tmp_path), factory)
    legacy = await executor.repository.create_job(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "legacy"},
            input_assets_json=[MediaInputAsset(asset_id=unsafe)],
        )
    )
    with pytest.raises((ValueError, FileNotFoundError)):
        await executor._build_parameters(legacy.job)


@pytest.mark.asyncio
async def test_relation_wins_over_legacy_projection(asset_context, tmp_path):
    service, factory = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="relation-wins",
    )
    jobs = MediaJobRepository(factory)
    created = await jobs.create_job_with_assets(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "safe"},
            input_assets_json=[MediaInputAsset(asset_id=asset.id)],
        )
    )
    created.job.input_assets_json = [{"asset_id": "historic.png"}]
    executor = RecoverableComfyUIExecutor(
        jobs,
        SimpleNamespace(),
        managed_asset_root=tmp_path,
        managed_output_root=tmp_path / "output",
        asset_service=service,
    )
    assert (await executor._build_parameters(created.job))["image_path"] == (
        service.store.local_path(asset.object_key)
    )


@pytest.mark.asyncio
async def test_legacy_symlink_escape_is_rejected_in_production_assembly(asset_context, tmp_path):
    _service, factory = asset_context
    executor = _build_executor(_production_manager(tmp_path), factory)
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = legacy_root / "historic.png"
    try:
        link.symlink_to(outside)
    except OSError:
        assert not link.exists()
        return
    legacy = await executor.repository.create_job(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "legacy"},
            input_assets_json=[MediaInputAsset(asset_id="historic.png")],
        )
    )
    with pytest.raises(ValueError, match="outside"):
        await executor._build_parameters(legacy.job)


@pytest.mark.asyncio
async def test_cleanup_claim_blocks_references_and_has_single_winner(asset_context):
    service, factory = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="cleanup-claim",
    )
    claimed = await service.repository.claim_cleanup(asset.id)
    assert claimed.state == AssetState.DISABLED.value
    with pytest.raises(RuntimeError):
        await service.repository.claim_cleanup(asset.id)
    with pytest.raises(ValueError):
        await MediaJobRepository(factory).create_job_with_assets(
            MediaJobCreate(
                workflow_type="gpu_4090_wan21_i2v_33f",
                workflow_key="workflow.json",
                executor_kind="private_comfyui",
                provider="private_comfyui",
                input_json={"prompt": "blocked"},
                input_assets_json=[MediaInputAsset(asset_id=asset.id)],
            )
        )
    assert await service.repository.reference_count(asset.id) == 0
    assert service.store.exists(asset.object_key)
    with pytest.raises(AssetUnavailableError):
        await service.open_content(asset.id)


@pytest.mark.asyncio
async def test_cleanup_claim_and_new_reference_cannot_both_win(asset_context):
    service, factory = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="cleanup-race",
    )
    jobs = MediaJobRepository(factory)
    create = MediaJobCreate(
        workflow_type="gpu_4090_wan21_i2v_33f",
        workflow_key="workflow.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        input_json={"prompt": "race"},
        input_assets_json=[MediaInputAsset(asset_id=asset.id)],
    )
    claim_result, create_result = await asyncio.gather(
        service.repository.claim_cleanup(asset.id),
        jobs.create_job_with_assets(create),
        return_exceptions=True,
    )
    claim_won = not isinstance(claim_result, Exception)
    create_won = not isinstance(create_result, Exception)
    assert claim_won != create_won
    persisted = await service.repository.get(asset.id)
    assert persisted is not None
    if claim_won:
        assert persisted.state == AssetState.DISABLED.value
        assert await service.repository.reference_count(asset.id) == 0
    else:
        assert persisted.state == AssetState.AVAILABLE.value
        assert await service.repository.reference_count(asset.id) == 1
    assert service.store.exists(asset.object_key)


@pytest.mark.asyncio
async def test_cleanup_file_failure_and_database_failure_remain_unavailable(
    asset_context, monkeypatch
):
    service, _factory = asset_context
    asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key="cleanup-failures",
    )
    original_delete = service.store.delete
    monkeypatch.setattr(service.store, "delete", lambda _key: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        await service.physical_cleanup(asset.id)
    assert (await service.get(asset.id)).state == AssetState.DISABLED.value
    assert service.store.exists(asset.object_key)

    monkeypatch.setattr(service.store, "delete", original_delete)
    async with service.repository._session_factory() as session:
        async with session.begin():
            await session.execute(
                update(MediaAsset)
                .where(MediaAsset.id == asset.id)
                .values(updated_at=utc_now() - timedelta(minutes=2))
            )
    original_purge = service.repository.purge_claimed_record

    async def fail_purge(_asset_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service.repository, "purge_claimed_record", fail_purge)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.physical_cleanup(asset.id)
    assert (await service.get(asset.id)).state == AssetState.DISABLED.value
    assert not service.store.exists(asset.object_key)
    with pytest.raises(AssetUnavailableError):
        await service.open_content(asset.id)

    monkeypatch.setattr(service.repository, "purge_claimed_record", original_purge)
    await service.physical_cleanup(asset.id)
    assert await service.repository.get(asset.id) is None


class _OutputAdapter:
    async def get_outputs(self, _remote_job):
        return [
            ComfyUIOutput(
                node_id="1",
                filename="result.mp4",
                subfolder="",
                storage_type="output",
                media_type="video",
                url="/view",
            )
        ]

    async def download_output(self, _output):
        return b"\x00\x00\x00\x18ftypisom" + b"x" * 16


class _Lease:
    def __init__(self, job, *, fail_call=None):
        self.job_id = job.job_id
        self.worker_id = "worker-1"
        self.status = JobStatus(job.status)
        self.version = job.version
        self.fail_call = fail_call
        self.calls = 0

    async def mutate(self, operation):
        self.calls += 1
        if self.calls == self.fail_call:
            raise LeaseLostError("injected lease loss")
        job = await operation(self.status, self.version)
        self.status = JobStatus(job.status)
        self.version = job.version
        return job


async def _running_output_job(service, factory, key):
    input_asset, _ = await service.upload(
        io.BytesIO(PNG),
        filename="client.png",
        mime_type="image/png",
        idempotency_key=f"{key}-input",
    )
    jobs = MediaJobRepository(factory)
    created = await jobs.create_job_with_assets(
        MediaJobCreate(
            workflow_type="gpu_4090_wan21_i2v_33f",
            workflow_key="workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            input_json={"prompt": "safe"},
            input_assets_json=[MediaInputAsset(asset_id=input_asset.id)],
        )
    )
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(type(created.job))
                .where(type(created.job).job_id == created.job.job_id)
                .values(status="running", lease_owner="worker-1")
            )
    return jobs, await jobs.get_job(created.job.job_id), input_asset


def _output_executor(service, jobs, tmp_path):
    return RecoverableComfyUIExecutor(
        jobs,
        _OutputAdapter(),
        managed_asset_root=tmp_path / "legacy",
        managed_output_root=tmp_path / "legacy-output",
        asset_service=service,
    )


@pytest.mark.asyncio
async def test_registered_outputs_survive_finish_lease_loss_and_recover(
    asset_context, tmp_path
):
    service, factory = asset_context
    jobs, running, input_asset = await _running_output_job(service, factory, "finish-loss")
    executor = _output_executor(service, jobs, tmp_path)
    with pytest.raises(LeaseLostError):
        await executor._complete(
            running,
            _Lease(running, fail_call=2),
            SimpleNamespace(),
        )
    persisted = await jobs.get_job(running.job_id)
    relations = await service.repository.output_assets_for_job(running.job_id)
    assert persisted.status == "running"
    assert len(relations) == 1
    assert relations[0][1].state == AssetState.AVAILABLE.value
    assert service.store.exists(relations[0][1].object_key)
    assert service.store.exists(input_asset.object_key)

    await executor._complete(persisted, _Lease(persisted), SimpleNamespace())
    finished = await jobs.get_job(running.job_id)
    assert finished.status == "succeeded"
    assert len(await service.repository.output_assets_for_job(running.job_id)) == 1
    assert service.store.exists(relations[0][1].object_key)


@pytest.mark.asyncio
async def test_recovery_rejects_committed_relation_with_missing_object(
    asset_context, tmp_path
):
    service, factory = asset_context
    jobs, running, _input = await _running_output_job(service, factory, "missing-recovery")
    executor = _output_executor(service, jobs, tmp_path)
    with pytest.raises(LeaseLostError):
        await executor._complete(running, _Lease(running, fail_call=2), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    output = (await service.repository.output_assets_for_job(running.job_id))[0][1]
    service.store.delete(output.object_key)
    await executor._complete(persisted, _Lease(persisted), SimpleNamespace())
    failed = await jobs.get_job(running.job_id)
    assert failed.status == "failed"
    assert failed.error_category == "output_missing"
    assert await service.repository.get(output.id) is not None


@pytest.mark.asyncio
async def test_recovery_rejects_inconsistent_output_projection(asset_context, tmp_path):
    service, factory = asset_context
    jobs, running, _input = await _running_output_job(service, factory, "projection-recovery")
    executor = _output_executor(service, jobs, tmp_path)
    with pytest.raises(LeaseLostError):
        await executor._complete(running, _Lease(running, fail_call=2), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(type(persisted))
                .where(type(persisted).job_id == persisted.job_id)
                .values(output_metadata=[{"output_id": "wrong"}])
            )
    persisted = await jobs.get_job(running.job_id)
    await executor._complete(persisted, _Lease(persisted), SimpleNamespace())
    failed = await jobs.get_job(running.job_id)
    assert failed.status == "failed"
    assert failed.error_category == "output_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    [
        "_stage_output_asset",
        "_stage_output_relation",
        "_project_output_metadata",
        "_flush_output_registration",
    ],
)
async def test_output_registration_failures_roll_back_compensate_and_fail_job(
    asset_context, tmp_path, monkeypatch, failure_point
):
    service, factory = asset_context
    jobs, running, input_asset = await _running_output_job(
        service, factory, f"register-{failure_point}"
    )
    executor = _output_executor(service, jobs, tmp_path)

    if failure_point == "_flush_output_registration":
        async def fail(*_args):
            raise RuntimeError("injected database failure")
    else:
        def fail(*_args):
            raise RuntimeError("injected database failure")
    monkeypatch.setattr(service.repository, failure_point, fail)

    await executor._complete(running, _Lease(running), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    assert persisted.status == "failed"
    assert persisted.output_metadata == []
    assert await service.repository.output_assets_for_job(running.job_id) == []
    assert [asset.kind for asset in await service.repository.all_assets()] == ["input"]
    assert list(service.store.iter_object_keys()) == [input_asset.object_key]


@pytest.mark.asyncio
async def test_multi_output_group_failure_leaves_no_partial_success(
    asset_context, tmp_path, monkeypatch
):
    service, factory = asset_context
    jobs, running, input_asset = await _running_output_job(service, factory, "multi-output")

    class TwoOutputAdapter(_OutputAdapter):
        async def get_outputs(self, _remote_job):
            outputs = await super().get_outputs(_remote_job)
            return outputs + [
                ComfyUIOutput(
                    node_id="2",
                    filename="second.mp4",
                    subfolder="",
                    storage_type="output",
                    media_type="video",
                    url="/view",
                )
            ]

    executor = RecoverableComfyUIExecutor(
        jobs,
        TwoOutputAdapter(),
        managed_asset_root=tmp_path / "legacy",
        managed_output_root=tmp_path / "legacy-output",
        asset_service=service,
    )
    original_stage = service.repository._stage_output_relation
    calls = 0

    def fail_second_relation(session, relation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second relation failed")
        original_stage(session, relation)

    monkeypatch.setattr(service.repository, "_stage_output_relation", fail_second_relation)
    await executor._complete(running, _Lease(running), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    assert persisted.status == "failed"
    assert persisted.output_metadata == []
    assert await service.repository.output_assets_for_job(running.job_id) == []
    assert [asset.kind for asset in await service.repository.all_assets()] == ["input"]
    assert list(service.store.iter_object_keys()) == [input_asset.object_key]


@pytest.mark.asyncio
async def test_commit_unknown_is_queried_before_compensation(asset_context, tmp_path, monkeypatch):
    service, factory = asset_context
    jobs, running, _input = await _running_output_job(service, factory, "commit-known")
    executor = _output_executor(service, jobs, tmp_path)
    original_commit = service.repository._commit_output_registration

    async def committed_then_error(transaction):
        await original_commit(transaction)
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(service.repository, "_commit_output_registration", committed_then_error)
    await executor._complete(running, _Lease(running), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    relations = await service.repository.output_assets_for_job(running.job_id)
    assert persisted.status == "succeeded"
    assert len(relations) == 1
    assert service.store.exists(relations[0][1].object_key)


@pytest.mark.asyncio
async def test_indeterminate_registration_never_guesses_by_deleting(
    asset_context, tmp_path, monkeypatch
):
    service, factory = asset_context
    jobs, running, input_asset = await _running_output_job(service, factory, "commit-unknown")
    executor = _output_executor(service, jobs, tmp_path)

    async def fail_commit(_transaction):
        raise RuntimeError("commit outcome unknown")

    async def classify_unknown(**_kwargs):
        return OutputRegistrationDisposition.UNKNOWN

    monkeypatch.setattr(service.repository, "_commit_output_registration", fail_commit)
    monkeypatch.setattr(service.repository, "_classify_output_registration", classify_unknown)
    await executor._complete(running, _Lease(running), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    assert persisted.status == "running"
    assert await service.repository.output_assets_for_job(running.job_id) == []
    keys = list(service.store.iter_object_keys())
    assert input_asset.object_key in keys
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_partial_persistence_is_indeterminate_and_preserves_object(
    asset_context, tmp_path, monkeypatch
):
    service, factory = asset_context
    jobs, running, input_asset = await _running_output_job(service, factory, "commit-partial")
    executor = _output_executor(service, jobs, tmp_path)
    original_classify = service.repository._classify_output_registration

    async def fail_commit(_transaction):
        raise RuntimeError("commit outcome unknown")

    async def create_partial_then_classify(*, job_id, asset_ids, output_metadata):
        projection = output_metadata[0]
        async with factory() as session:
            async with session.begin():
                session.add(
                    MediaAsset(
                        id=asset_ids[0],
                        kind="output",
                        state="available",
                        backend="local",
                        object_key=projection["relative_path"],
                        original_filename="result.mp4",
                        media_type=projection["media_type"],
                        mime_type=projection["mime_type"],
                        size_bytes=projection["size"],
                        sha256=projection["sha256"],
                        source="generated",
                    )
                )
        return await original_classify(
            job_id=job_id,
            asset_ids=asset_ids,
            output_metadata=output_metadata,
        )

    monkeypatch.setattr(service.repository, "_commit_output_registration", fail_commit)
    monkeypatch.setattr(
        service.repository,
        "_classify_output_registration",
        create_partial_then_classify,
    )
    await executor._complete(running, _Lease(running), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    assert persisted.status == "running"
    assert await service.repository.output_assets_for_job(running.job_id) == []
    assets = await service.repository.all_assets()
    output = next(asset for asset in assets if asset.kind == "output")
    assert service.store.exists(output.object_key)
    assert service.store.exists(input_asset.object_key)


@pytest.mark.asyncio
async def test_commit_failure_proven_uncommitted_compensates(asset_context, tmp_path, monkeypatch):
    service, factory = asset_context
    jobs, running, input_asset = await _running_output_job(service, factory, "commit-none")
    executor = _output_executor(service, jobs, tmp_path)

    async def fail_before_commit(_transaction):
        raise RuntimeError("database rejected commit")

    monkeypatch.setattr(service.repository, "_commit_output_registration", fail_before_commit)
    await executor._complete(running, _Lease(running), SimpleNamespace())
    persisted = await jobs.get_job(running.job_id)
    assert persisted.status == "failed"
    assert persisted.output_metadata == []
    assert await service.repository.output_assets_for_job(running.job_id) == []
    assert list(service.store.iter_object_keys()) == [input_asset.object_key]


@pytest.mark.asyncio
async def test_output_relation_rejects_nonavailable_asset(asset_context):
    service, factory = asset_context
    jobs, running, _input = await _running_output_job(service, factory, "unavailable-output")
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"x" * 16
    output = await service.register_generated_bytes(mp4, filename="result.mp4")
    output.state = AssetState.DISABLED.value
    metadata = MediaOutputMetadata(
        output_id=output.id,
        media_type="video",
        relative_path=output.object_key,
        size=output.size_bytes,
        mime_type=output.mime_type,
        sha256=output.sha256,
    )
    with pytest.raises(
        OutputRegistrationError,
        match=OutputRegistrationDisposition.NOT_COMMITTED.value,
    ):
        await service.repository.register_output_group(
            job_id=running.job_id,
            lease_owner="worker-1",
            expected_version=running.version,
            assets=[output],
            roles=["generated_video"],
            output_metadata=[metadata.model_dump()],
        )
    assert await service.repository.get(output.id) is None
    assert await service.repository.output_assets_for_job(running.job_id) == []
    service.discard_unregistered(output)
