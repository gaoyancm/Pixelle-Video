"""Short-transaction repository foundation for phase 03 management data."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.contracts import MediaJobCreate, compute_request_hash
from pixelle_video.media_jobs.models import MediaJob, utc_now
from pixelle_video.media_jobs.repository import MediaJobRepository

from .domain import (
    BatchState,
    OperationScopeKey,
    batch_content_is_editable,
    normalize_operation_scope_key,
    normalize_priority,
    operation_hash_matches,
    select_current_attempt,
)
from .models import (
    ManagementOperation,
    ProductionBatch,
    ProductionItem,
    ProductionItemAsset,
    ProductionItemAttempt,
    Project,
)


class ManagementRepositoryError(RuntimeError):
    """Stable management-domain persistence error."""


class ManagementNotFoundError(ManagementRepositoryError):
    pass


class ManagementConflictError(ManagementRepositoryError):
    pass


class ManagementConstraintError(ManagementRepositoryError):
    pass


class SubmissionIndeterminateError(ManagementRepositoryError):
    pass


class OperationMatchKind(str, Enum):
    MISSING = "missing"
    SAME_HASH = "same_hash"
    DIFFERENT_HASH = "different_hash"


@dataclass(frozen=True)
class OperationMatch:
    kind: OperationMatchKind
    operation: ManagementOperation | None


@dataclass(frozen=True)
class DraftAssetReference:
    asset_id: str
    role: str
    position: int


@dataclass(frozen=True)
class DraftItem:
    position: int
    parameter_overrides: dict[str, Any] = field(default_factory=dict)
    priority_override: int | None = None
    assets: tuple[DraftAssetReference, ...] = ()
    item_id: str | None = None


@dataclass(frozen=True)
class SubmissionItem:
    item_id: str
    effective_parameters: dict[str, Any]
    priority: int
    create: MediaJobCreate


@dataclass(frozen=True)
class BatchSubmission:
    batch_id: str
    expected_version: int
    idempotency_key: str
    request_hash: str
    operation_id: str
    items: tuple[SubmissionItem, ...]
    result_json: dict[str, Any]


@dataclass(frozen=True)
class BatchSubmissionResult:
    result_json: dict[str, Any]
    created: bool


class ManagementRepository:
    """Management persistence that can be standalone or transaction-bound."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ):
        self._session_factory = session_factory
        self._session = session

    def in_transaction(self) -> ManagementUnitOfWork:
        """Open one explicit transaction for later application-service composition."""

        if self._session is not None:
            raise RuntimeError("repository is already transaction-bound")
        return ManagementUnitOfWork(self._session_factory)

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[AsyncSession]:
        if self._session is not None:
            yield self._session
            return
        async with self._session_factory() as session:
            async with session.begin():
                yield session

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} must not be blank")
        return normalized

    @staticmethod
    def _constraint_error() -> ManagementConstraintError:
        return ManagementConstraintError("management persistence constraint violated")

    async def create_project(
        self,
        *,
        name: str,
        description: str | None = None,
        project_id: str | None = None,
    ) -> Project:
        project = Project(
            id=project_id or str(uuid.uuid4()),
            name=self._required_text(name, "project name"),
            description=description,
        )
        try:
            async with self._scope() as session:
                session.add(project)
                await session.flush()
        except IntegrityError:
            raise self._constraint_error() from None
        return project

    async def get_project(self, project_id: str) -> Project:
        async with self._scope() as session:
            project = await session.get(Project, project_id)
        if project is None:
            raise ManagementNotFoundError("project not found")
        return project

    async def list_projects(
        self, *, include_archived: bool = False, limit: int | None = None, offset: int = 0
    ) -> list[Project]:
        statement = select(Project)
        if not include_archived:
            statement = statement.where(Project.archived_at.is_(None))
        statement = statement.order_by(Project.updated_at.desc(), Project.id.desc()).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        async with self._scope() as session:
            return list((await session.execute(statement)).scalars())

    async def update_project(
        self,
        project_id: str,
        *,
        name: str,
        description: str | None,
    ) -> Project:
        statement = (
            update(Project)
            .where(Project.id == project_id, Project.archived_at.is_(None))
            .values(
                name=self._required_text(name, "project name"),
                description=description,
                updated_at=utc_now(),
            )
            .returning(Project)
        )
        async with self._scope() as session:
            project = (await session.execute(statement)).scalar_one_or_none()
        if project is None:
            existing = await self._project_exists(project_id)
            if existing:
                raise ManagementConflictError("archived project cannot be updated")
            raise ManagementNotFoundError("project not found")
        return project

    async def archive_project(self, project_id: str) -> Project:
        now = utc_now()
        statement = (
            update(Project)
            .where(Project.id == project_id, Project.archived_at.is_(None))
            .values(archived_at=now, updated_at=now)
            .returning(Project)
        )
        async with self._scope() as session:
            project = (await session.execute(statement)).scalar_one_or_none()
        if project is None:
            project = await self.get_project(project_id)
        return project

    async def _project_exists(self, project_id: str) -> bool:
        async with self._scope() as session:
            return await session.get(Project, project_id) is not None

    async def create_batch(
        self,
        *,
        project_id: str,
        name: str,
        workflow_type: str,
        common_parameters: dict[str, Any] | None = None,
        default_priority: int = 1,
        batch_id: str | None = None,
    ) -> ProductionBatch:
        batch = ProductionBatch(
            id=batch_id or str(uuid.uuid4()),
            project_id=project_id,
            name=self._required_text(name, "batch name"),
            workflow_type=self._required_text(workflow_type, "workflow type"),
            state=BatchState.DRAFT.value,
            common_parameters_json=common_parameters or {},
            default_priority=int(normalize_priority(default_priority)),
            version=1,
        )
        try:
            async with self._scope() as session:
                session.add(batch)
                await session.flush()
        except IntegrityError:
            raise self._constraint_error() from None
        return batch

    async def get_batch(self, batch_id: str) -> ProductionBatch:
        async with self._scope() as session:
            batch = await session.get(ProductionBatch, batch_id)
        if batch is None:
            raise ManagementNotFoundError("production batch not found")
        return batch

    async def list_batches(
        self,
        *,
        project_id: str,
        include_archived: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ProductionBatch]:
        statement = select(ProductionBatch).where(ProductionBatch.project_id == project_id)
        if not include_archived:
            statement = statement.where(ProductionBatch.archived_at.is_(None))
        statement = statement.order_by(
            ProductionBatch.created_at.desc(), ProductionBatch.id.desc()
        ).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        async with self._scope() as session:
            return list((await session.execute(statement)).scalars())

    async def update_draft_batch(
        self,
        batch_id: str,
        *,
        name: str,
        workflow_type: str,
        common_parameters: dict[str, Any],
        default_priority: int,
        expected_version: int | None = None,
    ) -> ProductionBatch:
        conditions = [
            ProductionBatch.id == batch_id,
            ProductionBatch.state == BatchState.DRAFT.value,
            ProductionBatch.archived_at.is_(None),
        ]
        if expected_version is not None:
            conditions.append(ProductionBatch.version == expected_version)
        statement = (
            update(ProductionBatch)
            .where(*conditions)
            .values(
                name=self._required_text(name, "batch name"),
                workflow_type=self._required_text(workflow_type, "workflow type"),
                common_parameters_json=common_parameters,
                default_priority=int(normalize_priority(default_priority)),
                updated_at=utc_now(),
                version=ProductionBatch.version + 1,
            )
            .returning(ProductionBatch)
        )
        async with self._scope() as session:
            batch = (await session.execute(statement)).scalar_one_or_none()
        if batch is None:
            await self._raise_batch_write_failure(batch_id)
        return batch

    async def archive_batch(self, batch_id: str) -> ProductionBatch:
        now = utc_now()
        statement = (
            update(ProductionBatch)
            .where(ProductionBatch.id == batch_id, ProductionBatch.archived_at.is_(None))
            .values(archived_at=now, updated_at=now, version=ProductionBatch.version + 1)
            .returning(ProductionBatch)
        )
        async with self._scope() as session:
            batch = (await session.execute(statement)).scalar_one_or_none()
        if batch is None:
            batch = await self.get_batch(batch_id)
        return batch

    async def _raise_batch_write_failure(self, batch_id: str) -> None:
        batch = await self.get_batch(batch_id)
        if not batch_content_is_editable(state=batch.state, archived_at=batch.archived_at):
            raise ManagementConflictError("production batch content is not editable")
        raise ManagementConflictError("production batch version changed")

    async def upsert_draft_items(
        self,
        batch_id: str,
        items: Sequence[DraftItem],
        *,
        replace: bool = False,
        expected_version: int | None = None,
    ) -> list[ProductionItem]:
        positions = [item.position for item in items]
        if len(positions) != len(set(positions)):
            raise ManagementConstraintError("item positions must be unique within a batch")
        if any(position < 0 for position in positions):
            raise ManagementConstraintError("item position must not be negative")
        for item in items:
            if item.priority_override is not None:
                normalize_priority(item.priority_override)
            relation_keys = [(asset.role, asset.position) for asset in item.assets]
            asset_keys = [(asset.asset_id, asset.role) for asset in item.assets]
            if len(relation_keys) != len(set(relation_keys)) or len(asset_keys) != len(
                set(asset_keys)
            ):
                raise ManagementConstraintError("draft asset relations must be unique")
            if any(asset.position < 0 for asset in item.assets):
                raise ManagementConstraintError("asset position must not be negative")

        written: list[ProductionItem] = []
        try:
            async with self._scope() as session:
                batch = await session.get(ProductionBatch, batch_id)
                if batch is None:
                    raise ManagementNotFoundError("production batch not found")
                if not batch_content_is_editable(state=batch.state, archived_at=batch.archived_at):
                    raise ManagementConflictError("production batch content is not editable")
                if expected_version is not None and batch.version != expected_version:
                    raise ManagementConflictError("production batch version changed")

                existing_items = list(
                    (
                        await session.execute(
                            select(ProductionItem).where(ProductionItem.batch_id == batch_id)
                        )
                    ).scalars()
                )
                existing_by_id = {item.id: item for item in existing_items}
                requested_ids = [draft.item_id for draft in items if draft.item_id is not None]
                if len(requested_ids) != len(set(requested_ids)):
                    raise ManagementConstraintError(
                        "production item may appear only once in an upsert"
                    )
                for item_id in requested_ids:
                    if item_id in existing_by_id:
                        continue
                    item = await session.get(ProductionItem, item_id)
                    if item is None:
                        raise ManagementNotFoundError("production item not found")
                    raise ManagementConflictError("production item belongs to another batch")

                requested_id_set = set(requested_ids)
                omitted = [item for item in existing_items if item.id not in requested_id_set]
                if replace and omitted:
                    attempted = await session.scalar(
                        select(func.count())
                        .select_from(ProductionItemAttempt)
                        .where(ProductionItemAttempt.item_id.in_([item.id for item in omitted]))
                    )
                    if attempted:
                        raise ManagementConflictError(
                            "submitted production items cannot be removed"
                        )
                    await session.execute(
                        delete(ProductionItemAsset).where(
                            ProductionItemAsset.item_id.in_([item.id for item in omitted])
                        )
                    )
                    await session.execute(
                        delete(ProductionItem).where(
                            ProductionItem.id.in_([item.id for item in omitted])
                        )
                    )
                    existing_items = [
                        item for item in existing_items if item.id in requested_id_set
                    ]
                    existing_by_id = {item.id: item for item in existing_items}
                retained_positions = {
                    item.position for item in existing_items if item.id not in requested_id_set
                }
                final_positions = retained_positions | set(positions)
                if len(final_positions) != len(retained_positions) + len(positions):
                    raise ManagementConstraintError("item positions must be unique within a batch")

                now = utc_now()
                changed_existing = [
                    (existing_by_id[draft.item_id], draft.position)
                    for draft in items
                    if draft.item_id is not None
                    and existing_by_id[draft.item_id].position != draft.position
                ]
                occupied_positions = {item.position for item in existing_items} | final_positions
                temporary_position = max(occupied_positions, default=-1) + 1
                for item, _ in changed_existing:
                    while temporary_position in occupied_positions:
                        temporary_position += 1
                    item.position = temporary_position
                    occupied_positions.add(temporary_position)
                    temporary_position += 1
                if changed_existing:
                    await session.flush()

                for draft in items:
                    item = existing_by_id.get(draft.item_id) if draft.item_id is not None else None
                    if item is None:
                        item = ProductionItem(
                            id=str(uuid.uuid4()),
                            batch_id=batch_id,
                            position=draft.position,
                            parameter_overrides_json=draft.parameter_overrides,
                            effective_parameters_json=None,
                            priority_override=draft.priority_override,
                        )
                        session.add(item)
                    else:
                        item.position = draft.position
                        item.parameter_overrides_json = draft.parameter_overrides
                        item.priority_override = draft.priority_override
                        item.updated_at = now
                    written.append(item)
                await session.flush()
                if replace:
                    batch.version += 1
                    batch.updated_at = utc_now()
                    await session.flush()

                for draft, item in zip(items, written, strict=True):
                    if draft.item_id is not None:
                        await session.execute(
                            delete(ProductionItemAsset).where(
                                ProductionItemAsset.item_id == item.id
                            )
                        )
                    session.add_all(
                        ProductionItemAsset(
                            item_id=item.id,
                            asset_id=asset.asset_id,
                            role=self._required_text(asset.role, "asset role"),
                            position=asset.position,
                        )
                        for asset in draft.assets
                    )
                await session.flush()
        except IntegrityError:
            raise self._constraint_error() from None
        return written

    async def replace_draft_items(
        self, batch_id: str, items: Sequence[DraftItem], *, expected_version: int
    ) -> tuple[list[ProductionItem], int]:
        written = await self.upsert_draft_items(
            batch_id, items, replace=True, expected_version=expected_version
        )
        batch = await self.get_batch(batch_id)
        return written, batch.version

    async def get_item(self, item_id: str) -> ProductionItem:
        async with self._scope() as session:
            item = await session.get(ProductionItem, item_id)
        if item is None:
            raise ManagementNotFoundError("production item not found")
        return item

    async def list_items(self, batch_id: str) -> list[ProductionItem]:
        statement = (
            select(ProductionItem)
            .where(ProductionItem.batch_id == batch_id)
            .order_by(ProductionItem.position.asc(), ProductionItem.id.asc())
        )
        async with self._scope() as session:
            return list((await session.execute(statement)).scalars())

    async def list_item_assets(self, item_id: str) -> list[ProductionItemAsset]:
        statement = (
            select(ProductionItemAsset)
            .where(ProductionItemAsset.item_id == item_id)
            .order_by(ProductionItemAsset.role.asc(), ProductionItemAsset.position.asc())
        )
        async with self._scope() as session:
            return list((await session.execute(statement)).scalars())

    async def register_attempt(
        self,
        *,
        item_id: str,
        attempt_no: int,
        media_job_id: str,
        retry_of_attempt_no: int | None = None,
    ) -> ProductionItemAttempt:
        if attempt_no < 1 or (retry_of_attempt_no is not None and retry_of_attempt_no < 1):
            raise ManagementConstraintError("attempt numbers must be greater than zero")
        attempt = ProductionItemAttempt(
            item_id=item_id,
            attempt_no=attempt_no,
            media_job_id=media_job_id,
            retry_of_attempt_no=retry_of_attempt_no,
        )
        try:
            async with self._scope() as session:
                session.add(attempt)
                await session.flush()
        except IntegrityError:
            raise self._constraint_error() from None
        return attempt

    async def list_attempts(self, item_id: str) -> list[ProductionItemAttempt]:
        statement = (
            select(ProductionItemAttempt)
            .where(ProductionItemAttempt.item_id == item_id)
            .order_by(ProductionItemAttempt.attempt_no.asc())
        )
        async with self._scope() as session:
            return list((await session.execute(statement)).scalars())

    async def get_current_attempt(self, item_id: str) -> ProductionItemAttempt | None:
        attempts = await self.list_attempts(item_id)
        current = select_current_attempt(attempts)
        return current if isinstance(current, ProductionItemAttempt) else None

    async def get_operation_match(
        self,
        *,
        scope_type: str,
        scope_id: str,
        operation_type: str,
        idempotency_key: str,
        request_hash: str,
    ) -> OperationMatch:
        key = normalize_operation_scope_key(
            scope_type=scope_type,
            scope_id=scope_id,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
        )
        operation = await self._get_operation(key)
        if operation is None:
            return OperationMatch(OperationMatchKind.MISSING, None)
        kind = (
            OperationMatchKind.SAME_HASH
            if operation_hash_matches(operation.request_hash, request_hash)
            else OperationMatchKind.DIFFERENT_HASH
        )
        return OperationMatch(kind, operation)

    async def get_operation(
        self,
        *,
        scope_type: str,
        scope_id: str,
        operation_type: str,
        idempotency_key: str,
    ) -> ManagementOperation | None:
        key = normalize_operation_scope_key(
            scope_type=scope_type,
            scope_id=scope_id,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
        )
        return await self._get_operation(key)

    async def create_operation(
        self,
        *,
        scope_type: str,
        scope_id: str,
        operation_type: str,
        idempotency_key: str,
        request_hash: str,
        result_json: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> OperationMatch:
        match = await self.get_operation_match(
            scope_type=scope_type,
            scope_id=scope_id,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if match.kind is OperationMatchKind.SAME_HASH:
            return match
        if match.kind is OperationMatchKind.DIFFERENT_HASH:
            raise ManagementConflictError("management operation key belongs to a different request")
        key = normalize_operation_scope_key(
            scope_type=scope_type,
            scope_id=scope_id,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
        )
        operation = ManagementOperation(
            id=operation_id or str(uuid.uuid4()),
            request_hash=request_hash,
            result_json=result_json,
            **key.__dict__,
        )
        try:
            async with self._scope() as session:
                session.add(operation)
                await session.flush()
        except IntegrityError:
            raise self._constraint_error() from None
        return OperationMatch(OperationMatchKind.MISSING, operation)

    async def submit_batch(
        self, submission: BatchSubmission, *, allow_retry: bool = True
    ) -> BatchSubmissionResult:
        """Create the operation, jobs, relations, attempts, and snapshots atomically."""

        if self._session is not None:
            raise RuntimeError("batch submission requires an unbound repository")
        session = self._session_factory()
        transaction = await session.begin()
        try:
            result = await self._stage_batch_submission(session, submission)
            if not result.created:
                await transaction.rollback()
                return result
            await self._commit_batch_submission(transaction)
            return result
        except (IntegrityError, DBAPIError, OSError):
            if transaction.is_active:
                await transaction.rollback()
            classified = await self._classify_batch_submission(submission)
            if classified is not None:
                return BatchSubmissionResult(classified, created=False)
            if allow_retry:
                return await self.submit_batch(submission, allow_retry=False)
            raise SubmissionIndeterminateError("batch submission outcome is unknown") from None
        finally:
            await session.close()

    @staticmethod
    async def _commit_batch_submission(transaction: Any) -> None:
        await transaction.commit()

    async def _stage_batch_submission(
        self, session: AsyncSession, submission: BatchSubmission
    ) -> BatchSubmissionResult:
        key = normalize_operation_scope_key(
            scope_type="batch",
            scope_id=submission.batch_id,
            operation_type="batch_submit",
            idempotency_key=submission.idempotency_key,
        )
        operation = (
            await session.execute(
                select(ManagementOperation).where(
                    ManagementOperation.scope_type == key.scope_type,
                    ManagementOperation.scope_id == key.scope_id,
                    ManagementOperation.operation_type == key.operation_type,
                    ManagementOperation.idempotency_key == key.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if operation is not None:
            if operation.request_hash != submission.request_hash:
                raise ManagementConflictError(
                    "management operation key belongs to a different request"
                )
            if operation.result_json is None:
                raise SubmissionIndeterminateError("batch submission result is incomplete")
            return BatchSubmissionResult(operation.result_json, created=False)

        batch = (
            await session.execute(
                select(ProductionBatch).where(ProductionBatch.id == submission.batch_id)
            )
        ).scalar_one_or_none()
        if batch is None:
            raise ManagementNotFoundError("production batch not found")
        if not batch_content_is_editable(state=batch.state, archived_at=batch.archived_at):
            raise ManagementConflictError("production batch content is not editable")
        if batch.version != submission.expected_version:
            raise ManagementConflictError("production batch version changed")

        persisted = list(
            (
                await session.execute(
                    select(ProductionItem)
                    .where(ProductionItem.batch_id == submission.batch_id)
                    .order_by(ProductionItem.position.asc(), ProductionItem.id.asc())
                )
            ).scalars()
        )
        if [item.id for item in persisted] != [item.item_id for item in submission.items]:
            raise ManagementConflictError("production batch items changed")

        from pixelle_video.media_assets.contracts import AssetDirection, AssetKind, AssetState
        from pixelle_video.media_assets.models import MediaAsset, MediaJobAsset

        operation = ManagementOperation(
            id=submission.operation_id,
            scope_type=key.scope_type,
            scope_id=key.scope_id,
            operation_type=key.operation_type,
            idempotency_key=key.idempotency_key,
            request_hash=submission.request_hash,
            result_json=submission.result_json,
        )
        session.add(operation)
        now = utc_now()
        staged: list[tuple[ProductionItem, SubmissionItem, MediaJob, list[Any]]] = []
        for row, planned in zip(persisted, submission.items, strict=True):
            relations = list(
                (
                    await session.execute(
                        select(ProductionItemAsset, MediaAsset)
                        .join(MediaAsset, MediaAsset.id == ProductionItemAsset.asset_id)
                        .where(ProductionItemAsset.item_id == row.id)
                        .order_by(
                            ProductionItemAsset.role.asc(), ProductionItemAsset.position.asc()
                        )
                    )
                ).tuples()
            )
            if any(
                asset.kind != AssetKind.INPUT.value or asset.state != AssetState.AVAILABLE.value
                for _, asset in relations
            ):
                raise ManagementConflictError("input asset is unavailable")
            job = MediaJobRepository._new_job(
                planned.create,
                compute_request_hash(planned.create.immutable_request_payload()),
            )
            session.add(job)
            staged.append((row, planned, job, relations))
        # SQLite foreign keys require every parent job to exist before relations.
        await session.flush()
        for row, planned, job, relations in staged:
            session.add_all(
                MediaJobAsset(
                    job_id=job.job_id,
                    asset_id=relation.asset_id,
                    direction=AssetDirection.INPUT.value,
                    role=relation.role,
                    position=relation.position,
                )
                for relation, _ in relations
            )
            session.add(
                ProductionItemAttempt(
                    item_id=row.id,
                    attempt_no=1,
                    media_job_id=job.job_id,
                )
            )
            row.effective_parameters_json = planned.effective_parameters
            row.updated_at = now

        batch.state = BatchState.SUBMITTED.value
        batch.submitted_at = now
        batch.updated_at = now
        batch.item_limit_snapshot = 100
        batch.version += 1
        await session.flush()
        return BatchSubmissionResult(submission.result_json, created=True)

    async def _classify_batch_submission(
        self, submission: BatchSubmission
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            operation = (
                await session.execute(
                    select(ManagementOperation).where(
                        ManagementOperation.scope_type == "batch",
                        ManagementOperation.scope_id == submission.batch_id,
                        ManagementOperation.operation_type == "batch_submit",
                        ManagementOperation.idempotency_key == submission.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if operation is None:
                return None
            if operation.request_hash != submission.request_hash:
                raise ManagementConflictError(
                    "management operation key belongs to a different request"
                )
            if operation.result_json != submission.result_json:
                raise SubmissionIndeterminateError("batch submission facts conflict")
            await self._validate_submission_result_in_session(
                session, submission.batch_id, operation.result_json
            )
            return operation.result_json

    async def validate_submission_result(self, batch_id: str, result_json: dict[str, Any]) -> None:
        async with self._session_factory() as session:
            await self._validate_submission_result_in_session(session, batch_id, result_json)

    @staticmethod
    async def _validate_submission_result_in_session(
        session: AsyncSession, batch_id: str, result_json: dict[str, Any]
    ) -> None:
        try:
            jobs = result_json["jobs"]
            job_ids = [item["job_id"] for item in jobs]
            item_ids = [item["item_id"] for item in jobs]
            result_batch_id = result_json["batch_id"]
            result_version = result_json["batch_version"]
            operation_id = result_json["operation_id"]
        except (KeyError, TypeError):
            raise SubmissionIndeterminateError("batch submission result is incomplete") from None
        if (
            result_batch_id != batch_id
            or not operation_id
            or not job_ids
            or len(job_ids) != len(set(job_ids))
            or len(item_ids) != len(set(item_ids))
        ):
            raise SubmissionIndeterminateError("batch submission result is inconsistent")
        job_count = await session.scalar(
            select(func.count()).select_from(MediaJob).where(MediaJob.job_id.in_(job_ids))
        )
        attempts = list(
            (
                await session.execute(
                    select(ProductionItemAttempt).where(
                        ProductionItemAttempt.media_job_id.in_(job_ids)
                    )
                )
            ).scalars()
        )
        batch = await session.get(ProductionBatch, batch_id)
        if (
            batch is None
            or batch.state != BatchState.SUBMITTED.value
            or batch.version != result_version
            or job_count != len(job_ids)
            or len(attempts) != len(job_ids)
            or {(attempt.item_id, attempt.media_job_id) for attempt in attempts}
            != set(zip(item_ids, job_ids, strict=True))
        ):
            raise SubmissionIndeterminateError("batch submission facts are incomplete")

    async def _get_operation(self, key: OperationScopeKey) -> ManagementOperation | None:
        statement = select(ManagementOperation).where(
            ManagementOperation.scope_type == key.scope_type,
            ManagementOperation.scope_id == key.scope_id,
            ManagementOperation.operation_type == key.operation_type,
            ManagementOperation.idempotency_key == key.idempotency_key,
        )
        async with self._scope() as session:
            return (await session.execute(statement)).scalar_one_or_none()


class ManagementUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._transaction = None

    async def __aenter__(self) -> ManagementRepository:
        self._session = self._session_factory()
        self._transaction = await self._session.begin()
        return ManagementRepository(self._session_factory, session=self._session)

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        assert self._session is not None and self._transaction is not None
        try:
            if exc_type is None:
                await self._transaction.commit()
            else:
                await self._transaction.rollback()
        finally:
            await self._session.close()
