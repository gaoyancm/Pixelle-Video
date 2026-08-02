from __future__ import annotations

import asyncio
import importlib
import io
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.app import app
from api.dependencies import get_management_service
from api.services.management import ManagementApplicationService
from pixelle_video.config.schema import ComfyUINodeConfig, MediaJobsConfig
from pixelle_video.management import (
    BatchSubmission,
    ManagementConflictError,
    ManagementRepository,
    SubmissionItem,
)
from pixelle_video.management.models import (
    ManagementOperation,
    ProductionBatch,
    ProductionItem,
    ProductionItemAttempt,
)
from pixelle_video.media_assets import AssetRepository, AssetService, LocalAssetStore
from pixelle_video.media_assets.models import MediaAsset, MediaJobAsset
from pixelle_video.media_jobs.contracts import MediaJobCreate
from pixelle_video.media_jobs.database import create_media_jobs_engine, sqlite_url_for_path
from pixelle_video.media_jobs.models import Base, MediaJob
from pixelle_video.media_jobs.state_machine import ErrorCategory

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


@pytest.fixture
async def management_context(tmp_path):
    engine = create_media_jobs_engine(sqlite_url_for_path(tmp_path / "management-api.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    assets = AssetService(
        AssetRepository(sessions), LocalAssetStore(tmp_path / "store"), max_upload_size=1024
    )
    nodes = [
        ComfyUINodeConfig(
            id="private-node",
            name="private",
            base_url="http://private.invalid",
            workflow_types=list(
                (
                    "a800_wan22_t2v_33f",
                    "a800_wan22_t2v_81f",
                    "gpu_4090_wan21_i2v_33f",
                    "gpu_4090_wan21_i2v_81f",
                )
            ),
            enabled=True,
        )
    ]
    repository = ManagementRepository(sessions)
    service = ManagementApplicationService(
        repository,
        MediaJobsConfig(enabled=True),
        assets,
        node_selector=lambda _workflow: "private-node",
        configured_nodes=nodes,
    )
    app.dependency_overrides[get_management_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, service, repository, assets, sessions
    app.dependency_overrides.clear()
    await engine.dispose()


async def create_batch(client, *, workflow="a800_wan22_t2v_33f", priority="normal"):
    project = await client.post(
        "/api/admin/projects", json={"name": "Project", "description": "safe"}
    )
    assert project.status_code == 201
    batch = await client.post(
        f"/api/admin/projects/{project.json()['project_id']}/batches",
        json={
            "name": "Batch",
            "workflow": workflow,
            "common_parameters": {"prompt": "safe prompt"},
            "default_priority": priority,
        },
    )
    assert batch.status_code == 201
    return project.json(), batch.json()


async def put_items(client, batch_id, items, *, version=1):
    return await client.put(
        f"/api/admin/batches/{batch_id}/items",
        json={"expected_batch_version": version, "items": items},
    )


async def create_submitted_batch(client, *, items=None):
    _, batch = await create_batch(client)
    items = items or [{"position": 0}]
    replaced = await put_items(client, batch["batch_id"], items)
    assert replaced.status_code == 200
    submitted = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers={"Idempotency-Key": f"submit-{batch['batch_id']}"},
    )
    assert submitted.status_code == 201
    return batch["batch_id"], replaced.json(), submitted.json()


@pytest.mark.asyncio
async def test_phase3c_priority_progress_and_safe_current_attempt_results(management_context):
    client, _, _, _, sessions = management_context
    batch_id, draft, submitted = await create_submitted_batch(
        client, items=[{"position": 0}, {"position": 1, "priority_override": "high"}]
    )
    priority = await client.patch(
        f"/api/admin/batches/{batch_id}/priority",
        json={"expected_batch_version": 3, "priority": "low"},
    )
    assert priority.status_code == 200
    assert priority.json()["default_priority"] == "low"
    assert priority.json()["updated"] == 1
    assert priority.json()["overridden"] == 1

    item_id = draft["items"][1]["item_id"]
    item_priority = await client.patch(
        f"/api/admin/items/{item_id}/priority",
        json={"expected_batch_version": 4, "priority": None},
    )
    assert item_priority.status_code == 200
    assert item_priority.json()["effective_priority"] == "low"

    first_job = submitted["jobs"][0]["job_id"]
    output_id = "11111111-1111-4111-8111-111111111111"
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == first_job)
            .values(
                status="succeeded",
                output_metadata=[
                    {
                        "output_id": output_id,
                        "width": 1280,
                        "height": 720,
                        "duration": 2.5,
                    }
                ],
            )
        )
        session.add(
            MediaAsset(
                id=output_id,
                kind="output",
                state="available",
                backend="local",
                object_key="generated/private/result.mp4",
                original_filename="result.mp4",
                media_type="video",
                mime_type="video/mp4",
                size_bytes=1234,
                sha256="a" * 64,
                source="generated",
            )
        )
        session.add(
            MediaJobAsset(
                job_id=first_job,
                asset_id=output_id,
                direction="output",
                role="video",
                position=0,
            )
        )
    progress = (await client.get(f"/api/admin/batches/{batch_id}/progress")).json()
    assert progress["batch_state"] == "submitted"
    assert progress["total_items"] == 2 and progress["completed_items"] == 1
    assert progress["derived_result"] is None and progress["counts"]["succeeded"] == 1
    results = (await client.get(f"/api/admin/batches/{batch_id}/results")).json()
    assert [item["position"] for item in results["items"]] == [0, 1]
    serialized = str(results).lower()
    for forbidden in ("node_id", "provider", "prompt_id", "object_key", "relative_path"):
        assert forbidden not in serialized
    output = results["items"][0]["attempts"][0]["outputs"][0]
    assert output["content_href"] == f"/api/assets/{output_id}/content"
    assert output["width"] == 1280 and output["duration"] == 2.5
    second_job = submitted["jobs"][1]["job_id"]
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob).where(MediaJob.job_id == second_job).values(status="cancelled")
        )
    completed = (await client.get(f"/api/admin/batches/{batch_id}/progress")).json()
    assert completed["completed_items"] == 2
    assert completed["derived_result"] == "partially_succeeded"
    assert completed["counts"]["cancelled"] == 1
    completed_results = (await client.get(f"/api/admin/batches/{batch_id}/results")).json()
    await client.post(f"/api/admin/batches/{batch_id}/archive")
    archived_results = await client.get(f"/api/admin/batches/{batch_id}/results")
    assert archived_results.status_code == 200 and archived_results.json() == completed_results


@pytest.mark.asyncio
async def test_phase3c_cancel_is_atomic_idempotent_and_never_interrupts_remote(management_context):
    client, _, _, _, sessions = management_context
    batch_id, _, submitted = await create_submitted_batch(client)
    headers = {"Idempotency-Key": "cancel-once"}
    first = await client.post(
        f"/api/admin/batches/{batch_id}/cancel", json={"expected_batch_version": 3}, headers=headers
    )
    replay = await client.post(
        f"/api/admin/batches/{batch_id}/cancel", json={"expected_batch_version": 3}, headers=headers
    )
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json() and first.json()["requested"] == 1
    async with sessions() as session:
        job = await session.get(MediaJob, submitted["jobs"][0]["job_id"])
        assert job.cancel_requested_at is not None and job.status == "queued"


@pytest.mark.asyncio
async def test_phase3c_retry_eligible_creates_one_lineage_and_zero_eligible_noop(
    management_context,
):
    client, _, _, _, sessions = management_context
    batch_id, draft, submitted = await create_submitted_batch(client)
    source_id = submitted["jobs"][0]["job_id"]
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == source_id)
            .values(status="failed", error_category=ErrorCategory.REMOTE_FAILED.value)
        )
    first = await client.post(
        f"/api/admin/batches/{batch_id}/retry-eligible",
        json={"expected_batch_version": 3},
        headers={"Idempotency-Key": "retry-one"},
    )
    replay = await client.post(
        f"/api/admin/batches/{batch_id}/retry-eligible",
        json={"expected_batch_version": 3},
        headers={"Idempotency-Key": "retry-one"},
    )
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json() and first.json()["retried"] == 1
    noop = await client.post(
        f"/api/admin/batches/{batch_id}/retry-eligible",
        json={"expected_batch_version": 3},
        headers={"Idempotency-Key": "retry-noop"},
    )
    assert noop.status_code == 201 and noop.json()["retried"] == 0
    results = (await client.get(f"/api/admin/batches/{batch_id}/results")).json()
    assert results["items"][0]["item_id"] == draft["items"][0]["item_id"]
    assert [attempt["attempt_no"] for attempt in results["items"][0]["attempts"]] == [1, 2]
    assert results["items"][0]["attempts"][1]["retry_of_attempt_no"] == 1
    progress = (await client.get(f"/api/admin/batches/{batch_id}/progress")).json()
    assert progress["total_items"] == 1 and progress["completed_items"] == 0
    assert progress["counts"]["queued"] == 1 and progress["derived_result"] is None


@pytest.mark.asyncio
async def test_phase3c_priority_version_cas_has_one_concurrent_winner(management_context):
    client, _, _, _, _ = management_context
    batch_id, _, _ = await create_submitted_batch(client)

    async def change(priority):
        return await client.patch(
            f"/api/admin/batches/{batch_id}/priority",
            json={"expected_batch_version": 3, "priority": priority},
        )

    responses = await asyncio.gather(change("low"), change("high"))
    assert sorted(response.status_code for response in responses) == [200, 409]


@pytest.mark.asyncio
async def test_phase3c_cancel_commit_unknown_is_classified_from_complete_facts(
    management_context, monkeypatch
):
    client, _, repository, _, sessions = management_context
    batch_id, _, submitted = await create_submitted_batch(client)

    async def committed_then_unknown(transaction):
        await transaction.commit()
        raise OSError("simulated lost acknowledgement")

    monkeypatch.setattr(repository, "_commit_management_operation", committed_then_unknown)
    response = await client.post(
        f"/api/admin/batches/{batch_id}/cancel",
        json={"expected_batch_version": 3},
        headers={"Idempotency-Key": "cancel-unknown"},
    )
    assert response.status_code == 200 and response.json()["requested"] == 1
    async with sessions() as session:
        job = await session.get(MediaJob, submitted["jobs"][0]["job_id"])
        assert job.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_project_batch_crud_archive_pagination_and_catalog_redaction(management_context):
    client, _, _, _, _ = management_context
    project, batch = await create_batch(client)
    listed = await client.get("/api/admin/projects", params={"limit": 1})
    assert listed.json()["items"][0]["project_id"] == project["project_id"]
    updated = await client.patch(
        f"/api/admin/batches/{batch['batch_id']}",
        json={
            "name": "Updated",
            "workflow": batch["workflow"],
            "common_parameters": {"prompt": "updated"},
            "default_priority": "high",
            "expected_batch_version": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    first_archive = await client.post(f"/api/admin/batches/{batch['batch_id']}/archive")
    replay_archive = await client.post(f"/api/admin/batches/{batch['batch_id']}/archive")
    assert first_archive.json() == replay_archive.json()

    catalog = (await client.get("/api/admin/workflows")).json()
    assert len(catalog["items"]) == 4
    serialized = str(catalog).lower()
    for forbidden in ("node_id", "base_url", ".json", "provider", "private-node"):
        assert forbidden not in serialized
    assert all(item["available"] for item in catalog["items"])


@pytest.mark.asyncio
async def test_archived_project_rejects_new_batch_and_errors_are_redacted(management_context):
    client, _, _, _, _ = management_context
    project = (await client.post("/api/admin/projects", json={"name": "P"})).json()
    await client.post(f"/api/admin/projects/{project['project_id']}/archive")
    response = await client.post(
        f"/api/admin/projects/{project['project_id']}/batches",
        json={"name": "B", "workflow": "a800_wan22_t2v_33f"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    assert "database" not in response.text.lower()


@pytest.mark.asyncio
async def test_catalog_and_preflight_report_no_enabled_node_without_connecting(
    management_context,
):
    client, service, _, _, _ = management_context
    service.configured_nodes = ()

    def unavailable(_workflow):
        raise RuntimeError("private node details")

    service.node_selector = unavailable
    _, batch = await create_batch(client)
    await put_items(client, batch["batch_id"], [{"position": 0}])
    catalog = (await client.get("/api/admin/workflows")).json()
    assert all(not item["available"] for item in catalog["items"])
    assert {item["unavailable_reason"] for item in catalog["items"]} == {"no_enabled_node"}
    preflight = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/preflight",
        json={"expected_batch_version": 2},
    )
    assert preflight.status_code == 200
    assert [issue["code"] for issue in preflight.json()["issues"]] == ["no_enabled_node"]
    assert "private" not in preflight.text.lower()


@pytest.mark.asyncio
async def test_item_complete_replacement_preserves_ids_and_preflight_is_read_only(
    management_context,
):
    client, _, _, _, sessions = management_context
    _, batch = await create_batch(client)
    first = await put_items(
        client,
        batch["batch_id"],
        [
            {"position": 0, "parameter_overrides": {"seed": 1}},
            {"position": 1, "parameter_overrides": {"seed": 2}},
        ],
    )
    assert first.status_code == 200
    ids = [item["item_id"] for item in first.json()["items"]]
    replaced = await put_items(
        client,
        batch["batch_id"],
        [{"item_id": ids[1], "position": 0, "parameter_overrides": {"seed": 3}}],
        version=2,
    )
    assert replaced.status_code == 200
    assert [item["item_id"] for item in replaced.json()["items"]] == [ids[1]]
    reloaded = await client.get(f"/api/admin/batches/{batch['batch_id']}")
    assert reloaded.status_code == 200
    assert reloaded.json()["items"] == replaced.json()["items"]

    async with sessions() as session:
        before = {
            model: await session.scalar(select(func.count()).select_from(model))
            for model in (ManagementOperation, MediaJob, ProductionItemAttempt)
        }
    preflight = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/preflight",
        json={"expected_batch_version": 3},
    )
    assert preflight.status_code == 200 and preflight.json()["valid"] is True
    async with sessions() as session:
        after = {
            model: await session.scalar(select(func.count()).select_from(model)) for model in before
        }
    assert after == before


@pytest.mark.asyncio
async def test_draft_allows_zero_but_preflight_requires_one_and_rejects_over_limit(
    management_context,
):
    client, _, _, _, _ = management_context
    _, batch = await create_batch(client)
    empty = await put_items(client, batch["batch_id"], [])
    assert empty.status_code == 200 and empty.json()["items"] == []
    preflight = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/preflight",
        json={"expected_batch_version": 2},
    )
    assert preflight.status_code == 200 and preflight.json()["valid"] is False
    assert preflight.json()["issues"][0]["code"] == "item_count_invalid"
    too_many = await put_items(
        client,
        batch["batch_id"],
        [{"position": index} for index in range(101)],
        version=2,
    )
    assert too_many.status_code == 422


@pytest.mark.asyncio
async def test_i2v_asset_contract_and_single_item_atomic_submit_replay(management_context):
    client, _, _, assets, sessions = management_context
    uploaded, _ = await assets.upload(
        io.BytesIO(PNG),
        filename="input.png",
        mime_type="image/png",
        idempotency_key="asset-api-test",
    )
    _, batch = await create_batch(client, workflow="gpu_4090_wan21_i2v_33f", priority="high")
    written = await put_items(
        client,
        batch["batch_id"],
        [
            {
                "position": 0,
                "parameter_overrides": {"seed": 7},
                "priority_override": "low",
                "assets": [{"asset_id": uploaded.id}],
            }
        ],
    )
    assert written.status_code == 200
    headers = {"Idempotency-Key": "batch-submit-one"}
    first = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers=headers,
    )
    replay = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers=headers,
    )
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["jobs"][0]["priority"] == "low"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 1
        assert await session.scalar(select(func.count()).select_from(MediaJobAsset)) == 1
        assert await session.scalar(select(func.count()).select_from(ProductionItemAttempt)) == 1
        stored = await session.get(MediaJob, first.json()["jobs"][0]["job_id"])
        item = (
            await session.execute(
                select(ProductionItem).where(ProductionItem.batch_id == batch["batch_id"])
            )
        ).scalar_one()
        persisted_batch = await session.get(ProductionBatch, batch["batch_id"])
    assert stored.priority == 0 and stored.node_id == "private-node"
    assert item.effective_parameters_json == {"prompt": "safe prompt", "seed": 7}
    assert persisted_batch.state == "submitted" and persisted_batch.item_limit_snapshot == 100

    conflict = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 3},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_hundred_items_submit_in_one_transaction_with_unique_scoped_keys(
    management_context,
):
    client, _, _, _, sessions = management_context
    _, batch = await create_batch(client)
    items = [{"position": index, "parameter_overrides": {"seed": index}} for index in range(100)]
    written = await put_items(client, batch["batch_id"], items)
    assert written.status_code == 200 and len(written.json()["items"]) == 100
    response = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers={"Idempotency-Key": "batch-submit-hundred"},
    )
    assert response.status_code == 201 and len(response.json()["jobs"]) == 100
    async with sessions() as session:
        jobs = list((await session.execute(select(MediaJob))).scalars())
        attempts = await session.scalar(select(func.count()).select_from(ProductionItemAttempt))
    assert len(jobs) == attempts == 100
    assert len({job.idempotency_key for job in jobs}) == 100
    assert all(len(job.idempotency_key) <= 255 for job in jobs)


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_every_fact_and_returns_indeterminate(
    management_context, monkeypatch
):
    client, _, repository, _, sessions = management_context
    _, batch = await create_batch(client)
    await put_items(client, batch["batch_id"], [{"position": 0}])

    async def fail_commit(_transaction):
        raise OSError("simulated commit failure")

    monkeypatch.setattr(repository, "_commit_batch_submission", fail_commit)
    response = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers={"Idempotency-Key": "rollback"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "submission_indeterminate"
    async with sessions() as session:
        for model in (ManagementOperation, MediaJob, MediaJobAsset, ProductionItemAttempt):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        item = (await session.execute(select(ProductionItem))).scalar_one()
        persisted_batch = await session.get(ProductionBatch, batch["batch_id"])
    assert item.effective_parameters_json is None
    assert persisted_batch.state == "draft" and persisted_batch.version == 2


@pytest.mark.asyncio
async def test_commit_unknown_with_complete_facts_is_classified_as_success(
    management_context, monkeypatch
):
    client, _, repository, _, sessions = management_context
    _, batch = await create_batch(client)
    await put_items(client, batch["batch_id"], [{"position": 0}])

    async def commit_then_raise(transaction):
        await transaction.commit()
        raise OSError("lost commit response")

    monkeypatch.setattr(repository, "_commit_batch_submission", commit_then_raise)
    response = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers={"Idempotency-Key": "commit-unknown-complete"},
    )
    assert response.status_code == 200
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 1


@pytest.mark.asyncio
async def test_same_hash_operation_with_incomplete_facts_is_indeterminate(management_context):
    client, service, repository, _, _ = management_context
    _, batch = await create_batch(client)
    await put_items(client, batch["batch_id"], [{"position": 0}])
    prepared = await service._prepare(batch["batch_id"], expected_version=2, select_node=False)
    request_hash = service._canonical_hash(prepared, 2)
    await repository.create_operation(
        scope_type="batch",
        scope_id=batch["batch_id"],
        operation_type="batch_submit",
        idempotency_key="partial-operation",
        request_hash=request_hash,
        result_json={
            "batch_id": batch["batch_id"],
            "batch_version": 3,
            "operation_id": "partial",
            "jobs": [{"item_id": prepared.items[0].item_id, "job_id": "missing"}],
        },
    )
    response = await client.post(
        f"/api/admin/batches/{batch['batch_id']}/submit",
        json={"expected_batch_version": 2},
        headers={"Idempotency-Key": "partial-operation"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "submission_indeterminate"


@pytest.mark.asyncio
async def test_concurrent_same_key_converges_to_one_job_group(management_context, monkeypatch):
    client, _, repository, _, sessions = management_context
    _, batch = await create_batch(client)
    await put_items(client, batch["batch_id"], [{"position": 0}])

    original_stage = repository._stage_batch_submission
    ready = asyncio.Event()
    arrivals = 0

    async def synchronized_stage(session, submission):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        return await original_stage(session, submission)

    monkeypatch.setattr(repository, "_stage_batch_submission", synchronized_stage)

    async def submit():
        return await client.post(
            f"/api/admin/batches/{batch['batch_id']}/submit",
            json={"expected_batch_version": 2},
            headers={"Idempotency-Key": "concurrent-batch"},
        )

    responses = await asyncio.gather(submit(), submit())
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json() == responses[1].json()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_different_hash_has_one_group_and_one_conflict(
    management_context, monkeypatch
):
    client, _, repository, _, sessions = management_context
    _, batch = await create_batch(client)
    written = await put_items(client, batch["batch_id"], [{"position": 0}])
    item_id = written.json()["items"][0]["item_id"]

    def submission(marker: str) -> BatchSubmission:
        operation_id = f"00000000-0000-4000-8000-00000000000{marker}"
        job_id = f"10000000-0000-4000-8000-00000000000{marker}"
        result = {
            "batch_id": batch["batch_id"],
            "batch_version": 3,
            "operation_id": operation_id,
            "jobs": [{"item_id": item_id, "job_id": job_id}],
        }
        create = MediaJobCreate(
            job_id=job_id,
            workflow_type="a800_wan22_t2v_33f",
            workflow_key="selfhost/workflow.json",
            executor_kind="private_comfyui",
            provider="private_comfyui",
            node_id="private-node",
            input_json={"prompt": "safe prompt"},
            idempotency_key=f"scoped-{marker}",
        )
        return BatchSubmission(
            batch_id=batch["batch_id"],
            expected_version=2,
            idempotency_key="same-key",
            request_hash=marker * 64,
            operation_id=operation_id,
            items=(
                SubmissionItem(
                    item_id=item_id,
                    effective_parameters={"prompt": "safe prompt"},
                    priority=1,
                    create=create,
                ),
            ),
            result_json=result,
        )

    original_stage = repository._stage_batch_submission
    ready = asyncio.Event()
    arrivals = 0

    async def synchronized_stage(session, planned):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        return await original_stage(session, planned)

    monkeypatch.setattr(repository, "_stage_batch_submission", synchronized_stage)
    results = await asyncio.gather(
        repository.submit_batch(submission("1")),
        repository.submit_batch(submission("2")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ManagementConflictError) for result in results) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 1
        assert await session.scalar(select(func.count()).select_from(ManagementOperation)) == 1


def test_openapi_has_exactly_twenty_authorized_phase3_management_operations():
    schema = app.openapi()
    paths = {
        path: methods for path, methods in schema["paths"].items() if path.startswith("/api/admin")
    }
    assert sum(len(methods) for methods in paths.values()) == 20
    serialized = str(paths).lower()
    for forbidden in ("node_id", "provider", "prompt_id", "submission_token", "base_url"):
        assert forbidden not in serialized
    assert {
        ("patch", "/api/admin/batches/{batch_id}/priority"),
        ("patch", "/api/admin/items/{item_id}/priority"),
        ("get", "/api/admin/batches/{batch_id}/progress"),
        ("get", "/api/admin/batches/{batch_id}/results"),
        ("post", "/api/admin/batches/{batch_id}/cancel"),
        ("post", "/api/admin/batches/{batch_id}/retry-eligible"),
    }.issubset({(method, path) for path, methods in paths.items() for method in methods})


def test_management_router_has_no_worker_provider_or_import_side_effect(monkeypatch):
    import api.dependencies as dependencies

    def forbidden_connect(self):
        raise AssertionError("router import must not connect to storage")

    monkeypatch.setattr(dependencies.MediaJobsDatabase, "connect", forbidden_connect)
    module = importlib.reload(importlib.import_module("api.routers.management"))
    assert len(module.router.routes) == 20
    source = Path("api/routers/management.py").read_text(encoding="utf-8")
    for forbidden in ("MediaJobWorker", "ComfyUIAdapter", "Provider", "AsyncSession"):
        assert forbidden not in source
