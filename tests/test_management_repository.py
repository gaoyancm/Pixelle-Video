from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.management import (
    DraftAssetReference,
    DraftItem,
    ManagementConflictError,
    ManagementConstraintError,
    ManagementNotFoundError,
    ManagementRepository,
    OperationMatchKind,
)
from pixelle_video.management.models import (
    ManagementOperation,
    ProductionBatch,
    ProductionItem,
    ProductionItemAsset,
    ProductionItemAttempt,
    Project,
)
from pixelle_video.media_assets.models import MediaAsset
from pixelle_video.media_jobs.database import create_media_jobs_engine, sqlite_url_for_path
from pixelle_video.media_jobs.models import Base, MediaJob, utc_now


async def open_repository(
    path: Path,
) -> tuple[ManagementRepository, object, async_sessionmaker[AsyncSession]]:
    engine = create_media_jobs_engine(sqlite_url_for_path(path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return ManagementRepository(sessions), engine, sessions


def asset(asset_id: str) -> MediaAsset:
    return MediaAsset(
        id=asset_id,
        kind="input",
        state="available",
        backend="local",
        object_key=f"objects/{asset_id}.png",
        original_filename="input.png",
        media_type="image",
        mime_type="image/png",
        size_bytes=1,
        sha256="a" * 64,
        source="upload",
    )


def job(job_id: str) -> MediaJob:
    return MediaJob(
        job_id=job_id,
        workflow_type="workflow",
        workflow_key="workflow.json",
        executor_kind="private_comfyui",
        provider="private_comfyui",
        status="queued",
        input_json={},
        input_assets_json=[],
        submission_token=uuid.uuid4().hex,
        request_hash="b" * 64,
        output_metadata=[],
        retry_count=0,
        version=1,
        remote_status="unknown",
        remote_termination_status="unknown",
    )


@pytest.mark.asyncio
async def test_project_crud_archive_and_default_filter(tmp_path):
    repository, engine, _ = await open_repository(tmp_path / "projects.db")
    project = await repository.create_project(name="Project", description="one")
    assert (await repository.get_project(project.id)).description == "one"
    updated = await repository.update_project(project.id, name="Renamed", description=None)
    assert updated.name == "Renamed"
    archived = await repository.archive_project(project.id)
    assert archived.archived_at is not None
    assert await repository.list_projects() == []
    assert [item.id for item in await repository.list_projects(include_archived=True)] == [
        project.id
    ]
    with pytest.raises(ManagementConflictError):
        await repository.update_project(project.id, name="Blocked", description=None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_batch_draft_update_archive_and_stable_list(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "batches.db")
    project = await repository.create_project(name="Project")
    first = await repository.create_batch(
        project_id=project.id, name="First", workflow_type="workflow"
    )
    second = await repository.create_batch(
        project_id=project.id, name="Second", workflow_type="workflow"
    )
    same_time = utc_now()
    async with sessions() as session, session.begin():
        await session.execute(update(ProductionBatch).values(created_at=same_time))
    listed = await repository.list_batches(project_id=project.id)
    assert [batch.id for batch in listed] == sorted([first.id, second.id], reverse=True)
    updated = await repository.update_draft_batch(
        first.id,
        name="Updated",
        workflow_type="workflow",
        common_parameters={"prompt": "safe"},
        default_priority=2,
        expected_version=1,
    )
    assert updated.default_priority == 2 and updated.version == 2
    await repository.archive_batch(first.id)
    assert [batch.id for batch in await repository.list_batches(project_id=project.id)] == [
        second.id
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_submitted_and_archived_batch_content_cannot_change(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "immutable.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    async with sessions() as session, session.begin():
        await session.execute(
            update(ProductionBatch).where(ProductionBatch.id == batch.id).values(state="submitted")
        )
    with pytest.raises(ManagementConflictError):
        await repository.update_draft_batch(
            batch.id,
            name="No",
            workflow_type="workflow",
            common_parameters={},
            default_priority=1,
        )
    with pytest.raises(ManagementConflictError):
        await repository.upsert_draft_items(batch.id, [DraftItem(position=0)])
    await engine.dispose()


@pytest.mark.asyncio
async def test_draft_item_and_asset_relations_round_trip_and_replace(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "items.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    first_asset, second_asset = str(uuid.uuid4()), str(uuid.uuid4())
    async with sessions() as session, session.begin():
        session.add_all([asset(first_asset), asset(second_asset)])
    created = (
        await repository.upsert_draft_items(
            batch.id,
            [
                DraftItem(
                    position=0,
                    parameter_overrides={"seed": 1},
                    priority_override=2,
                    assets=(DraftAssetReference(first_asset, "input_image", 0),),
                )
            ],
        )
    )[0]
    assert (await repository.list_items(batch.id))[0].priority_override == 2
    assert [row.asset_id for row in await repository.list_item_assets(created.id)] == [first_asset]
    await repository.upsert_draft_items(
        batch.id,
        [
            DraftItem(
                item_id=created.id,
                position=0,
                assets=(DraftAssetReference(second_asset, "input_image", 0),),
            )
        ],
    )
    assert [row.asset_id for row in await repository.list_item_assets(created.id)] == [second_asset]
    await engine.dispose()


@pytest.mark.asyncio
async def test_draft_items_can_swap_positions_without_changing_ids_or_assets(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "item-swap.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    first_asset, second_asset = str(uuid.uuid4()), str(uuid.uuid4())
    async with sessions() as session, session.begin():
        session.add_all([asset(first_asset), asset(second_asset)])
    first, second = await repository.upsert_draft_items(
        batch.id,
        [
            DraftItem(
                position=0,
                parameter_overrides={"label": "first"},
                assets=(DraftAssetReference(first_asset, "input_image", 0),),
            ),
            DraftItem(
                position=1,
                parameter_overrides={"label": "second"},
                assets=(DraftAssetReference(second_asset, "input_image", 0),),
            ),
        ],
    )

    swapped = await repository.upsert_draft_items(
        batch.id,
        [
            DraftItem(
                item_id=first.id,
                position=1,
                parameter_overrides={"label": "first-updated"},
                assets=(DraftAssetReference(first_asset, "input_image", 0),),
            ),
            DraftItem(
                item_id=second.id,
                position=0,
                parameter_overrides={"label": "second-updated"},
                assets=(DraftAssetReference(second_asset, "input_image", 0),),
            ),
        ],
    )

    assert {item.id for item in swapped} == {first.id, second.id}
    persisted = {item.id: item for item in await repository.list_items(batch.id)}
    assert persisted[first.id].position == 1
    assert persisted[second.id].position == 0
    assert persisted[first.id].parameter_overrides_json == {"label": "first-updated"}
    assert persisted[second.id].parameter_overrides_json == {"label": "second-updated"}
    assert [row.asset_id for row in await repository.list_item_assets(first.id)] == [first_asset]
    assert [row.asset_id for row in await repository.list_item_assets(second.id)] == [second_asset]
    await engine.dispose()


@pytest.mark.asyncio
async def test_draft_items_can_rotate_three_positions_and_update_fields(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "item-cycle.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    asset_ids = [str(uuid.uuid4()) for _ in range(3)]
    async with sessions() as session, session.begin():
        session.add_all(asset(asset_id) for asset_id in asset_ids)
    created = await repository.upsert_draft_items(
        batch.id,
        [
            DraftItem(
                position=index,
                parameter_overrides={"version": 1},
                assets=(DraftAssetReference(asset_id, "input_image", 0),),
            )
            for index, asset_id in enumerate(asset_ids)
        ],
    )

    rotated_positions = [1, 2, 0]
    await repository.upsert_draft_items(
        batch.id,
        [
            DraftItem(
                item_id=item.id,
                position=rotated_positions[index],
                parameter_overrides={"version": 2, "item": index},
                priority_override=index,
                assets=(DraftAssetReference(asset_ids[index], "input_image", 0),),
            )
            for index, item in enumerate(created)
        ],
    )

    persisted = {item.id: item for item in await repository.list_items(batch.id)}
    assert set(persisted) == {item.id for item in created}
    for index, item in enumerate(created):
        assert persisted[item.id].position == rotated_positions[index]
        assert persisted[item.id].parameter_overrides_json == {"version": 2, "item": index}
        assert persisted[item.id].priority_override == index
        assert [row.asset_id for row in await repository.list_item_assets(item.id)] == [
            asset_ids[index]
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_final_positions_roll_back_fields_positions_and_assets(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "item-duplicate-rollback.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    first_asset, second_asset = str(uuid.uuid4()), str(uuid.uuid4())
    async with sessions() as session, session.begin():
        session.add_all([asset(first_asset), asset(second_asset)])
    first, second = await repository.upsert_draft_items(
        batch.id,
        [
            DraftItem(
                position=0,
                parameter_overrides={"label": "first"},
                priority_override=0,
                assets=(DraftAssetReference(first_asset, "input_image", 0),),
            ),
            DraftItem(
                position=1,
                parameter_overrides={"label": "second"},
                priority_override=1,
                assets=(DraftAssetReference(second_asset, "input_image", 0),),
            ),
        ],
    )

    with pytest.raises(ManagementConstraintError):
        await repository.upsert_draft_items(
            batch.id,
            [
                DraftItem(
                    item_id=first.id,
                    position=1,
                    parameter_overrides={"label": "changed-first"},
                    priority_override=2,
                    assets=(DraftAssetReference(second_asset, "input_image", 0),),
                ),
                DraftItem(
                    item_id=second.id,
                    position=1,
                    parameter_overrides={"label": "changed-second"},
                    priority_override=2,
                    assets=(DraftAssetReference(first_asset, "input_image", 0),),
                ),
            ],
        )

    persisted = {item.id: item for item in await repository.list_items(batch.id)}
    assert persisted[first.id].position == 0
    assert persisted[second.id].position == 1
    assert persisted[first.id].parameter_overrides_json == {"label": "first"}
    assert persisted[second.id].parameter_overrides_json == {"label": "second"}
    assert persisted[first.id].priority_override == 0
    assert persisted[second.id].priority_override == 1
    assert [row.asset_id for row in await repository.list_item_assets(first.id)] == [first_asset]
    assert [row.asset_id for row in await repository.list_item_assets(second.id)] == [second_asset]
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relations",
    [
        (("asset-1", "input", 0), ("asset-2", "input", 0)),
        (("asset-1", "input", 0), ("asset-1", "input", 1)),
    ],
)
async def test_each_draft_asset_uniqueness_rule_is_enforced(tmp_path, relations):
    repository, engine, sessions = await open_repository(tmp_path / f"asset-unique-{relations}.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    async with sessions() as session, session.begin():
        session.add_all([asset("asset-1"), asset("asset-2")])
    with pytest.raises(ManagementConstraintError):
        await repository.upsert_draft_items(
            batch.id,
            [
                DraftItem(
                    position=0,
                    assets=tuple(DraftAssetReference(*relation) for relation in relations),
                )
            ],
        )
    assert await repository.list_items(batch.id) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_item_upsert_is_atomic_and_maps_constraint_error(tmp_path):
    repository, engine, _ = await open_repository(tmp_path / "item-atomic.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    with pytest.raises(ManagementConstraintError) as caught:
        await repository.upsert_draft_items(
            batch.id,
            [
                DraftItem(position=0),
                DraftItem(position=1, assets=(DraftAssetReference("missing", "input", 0),)),
            ],
        )
    assert "FOREIGN KEY" not in str(caught.value)
    assert await repository.list_items(batch.id) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_attempt_history_current_selection_and_global_job_uniqueness(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "attempts.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    items = await repository.upsert_draft_items(
        batch.id, [DraftItem(position=0), DraftItem(position=1)]
    )
    first_job, second_job = str(uuid.uuid4()), str(uuid.uuid4())
    async with sessions() as session, session.begin():
        session.add_all([job(first_job), job(second_job)])
    await repository.register_attempt(item_id=items[0].id, attempt_no=1, media_job_id=first_job)
    await repository.register_attempt(
        item_id=items[0].id,
        attempt_no=2,
        media_job_id=second_job,
        retry_of_attempt_no=1,
    )
    assert (await repository.get_current_attempt(items[0].id)).attempt_no == 2
    assert [row.attempt_no for row in await repository.list_attempts(items[0].id)] == [1, 2]
    with pytest.raises(ManagementConstraintError):
        await repository.register_attempt(
            item_id=items[1].id, attempt_no=1, media_job_id=second_job
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_management_operation_scope_hash_and_reuse_rules(tmp_path):
    repository, engine, _ = await open_repository(tmp_path / "operations.db")
    created = await repository.create_operation(
        scope_type="batch",
        scope_id="batch-1",
        operation_type="batch_submit",
        idempotency_key="key",
        request_hash="a" * 64,
    )
    assert created.operation is not None
    same = await repository.get_operation_match(
        scope_type="batch",
        scope_id="batch-1",
        operation_type="batch_submit",
        idempotency_key="key",
        request_hash="a" * 64,
    )
    assert same.kind is OperationMatchKind.SAME_HASH
    conflict = await repository.get_operation_match(
        scope_type="batch",
        scope_id="batch-1",
        operation_type="batch_submit",
        idempotency_key="key",
        request_hash="b" * 64,
    )
    assert conflict.kind is OperationMatchKind.DIFFERENT_HASH
    different_scope = await repository.create_operation(
        scope_type="batch",
        scope_id="batch-2",
        operation_type="batch_submit",
        idempotency_key="key",
        request_hash="b" * 64,
    )
    assert different_scope.operation is not None
    different_operation = await repository.create_operation(
        scope_type="batch",
        scope_id="batch-1",
        operation_type="batch_cancel",
        idempotency_key="key",
        request_hash="c" * 64,
    )
    assert different_operation.operation is not None
    with pytest.raises(ManagementConflictError):
        await repository.create_operation(
            scope_type="batch",
            scope_id="batch-1",
            operation_type="batch_submit",
            idempotency_key="key",
            request_hash="b" * 64,
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_unit_of_work_composes_and_rolls_back(tmp_path):
    repository, engine, _ = await open_repository(tmp_path / "uow.db")
    with pytest.raises(RuntimeError, match="rollback"):
        async with repository.in_transaction() as transaction:
            await transaction.create_project(name="Will roll back", project_id="project-1")
            raise RuntimeError("rollback")
    with pytest.raises(ManagementNotFoundError):
        await repository.get_project("project-1")
    async with repository.in_transaction() as transaction:
        await transaction.create_project(name="Committed", project_id="project-2")
        await transaction.create_batch(
            project_id="project-2",
            name="Batch",
            workflow_type="workflow",
            batch_id="batch-2",
        )
    assert (await repository.get_batch("batch-2")).project_id == "project-2"
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_checks_reject_invalid_states_priorities_and_positions(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "checks.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    item = (await repository.upsert_draft_items(batch.id, [DraftItem(position=0)]))[0]
    job_id = str(uuid.uuid4())
    async with sessions() as session, session.begin():
        session.add(job(job_id))
    statements = [
        ("UPDATE production_batches SET state='invalid' WHERE id=:id", {"id": batch.id}),
        ("UPDATE production_batches SET default_priority=9 WHERE id=:id", {"id": batch.id}),
        ("UPDATE media_jobs SET priority=9 WHERE job_id=:id", {"id": job_id}),
        ("UPDATE production_items SET priority_override=9 WHERE id=:id", {"id": item.id}),
        (
            "INSERT INTO production_items (id,batch_id,position,parameter_overrides_json,created_at,updated_at) "
            "VALUES ('bad-position',:id,-1,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            {"id": batch.id},
        ),
        (
            "INSERT INTO production_items (id,batch_id,position,parameter_overrides_json,created_at,updated_at) "
            "VALUES ('duplicate-position',:id,0,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            {"id": batch.id},
        ),
    ]
    for sql, params in statements:
        with pytest.raises(Exception):
            async with sessions() as session, session.begin():
                await session.execute(text(sql), params)
    await engine.dispose()


@pytest.mark.asyncio
async def test_foreign_keys_restrict_physical_parent_asset_and_job_deletion(tmp_path):
    repository, engine, sessions = await open_repository(tmp_path / "restrict.db")
    project = await repository.create_project(name="Project")
    batch = await repository.create_batch(
        project_id=project.id, name="Batch", workflow_type="workflow"
    )
    asset_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with sessions() as session, session.begin():
        session.add_all([asset(asset_id), job(job_id)])
    item = (
        await repository.upsert_draft_items(
            batch.id,
            [
                DraftItem(
                    position=0,
                    assets=(DraftAssetReference(asset_id, "input", 0),),
                )
            ],
        )
    )[0]
    await repository.register_attempt(item_id=item.id, attempt_no=1, media_job_id=job_id)
    for statement, parameters in (
        ("DELETE FROM projects WHERE id=:id", {"id": project.id}),
        ("DELETE FROM production_batches WHERE id=:id", {"id": batch.id}),
        ("DELETE FROM production_items WHERE id=:id", {"id": item.id}),
        ("DELETE FROM media_assets WHERE id=:id", {"id": asset_id}),
        ("DELETE FROM media_jobs WHERE job_id=:id", {"id": job_id}),
    ):
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await session.execute(text(statement), parameters)
    await engine.dispose()


def test_management_repository_exposes_no_physical_delete_methods():
    public = {name for name in dir(ManagementRepository) if not name.startswith("_")}
    assert (
        not {"delete_project", "delete_batch", "delete_item", "delete_attempt", "delete_operation"}
        & public
    )
    assert Project.__tablename__ == "projects"
    assert ProductionItem.__tablename__ == "production_items"
    assert ProductionItemAsset.__tablename__ == "production_item_assets"
    assert ProductionItemAttempt.__tablename__ == "production_item_attempts"
    assert ManagementOperation.__tablename__ == "management_operations"
