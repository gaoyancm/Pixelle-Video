"""Short-transaction repository for assets and job relations."""

from __future__ import annotations

from enum import Enum

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.models import MediaJob, utc_now

from .contracts import AssetDirection, AssetKind, AssetState
from .models import MediaAsset, MediaJobAsset, OutputSchema


class AssetIdempotencyConflictError(RuntimeError):
    pass


class AssetReferencedError(RuntimeError):
    pass


class OutputRegistrationDisposition(str, Enum):
    NOT_COMMITTED = "not_committed"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class OutputRegistrationError(RuntimeError):
    def __init__(self, disposition: OutputRegistrationDisposition):
        super().__init__(f"output registration {disposition.value}")
        self.disposition = disposition


class AssetRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create(self, asset: MediaAsset) -> tuple[MediaAsset, bool]:
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    session.add(asset)
                    await session.flush()
                return asset, True
            except IntegrityError:
                await session.rollback()
                if not asset.idempotency_key:
                    raise
                result = await session.execute(
                    select(MediaAsset).where(
                        MediaAsset.idempotency_key == asset.idempotency_key
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    raise
                if existing.request_hash != asset.request_hash:
                    raise AssetIdempotencyConflictError from None
                return existing, False

    async def get(self, asset_id: str) -> MediaAsset | None:
        async with self._session_factory() as session:
            return await session.get(MediaAsset, asset_id)

    async def list(
        self,
        *,
        kind: AssetKind | None,
        state: AssetState | None,
        limit: int,
        offset: int,
    ) -> list[MediaAsset]:
        statement = select(MediaAsset)
        if kind:
            statement = statement.where(MediaAsset.kind == kind.value)
        if state:
            statement = statement.where(MediaAsset.state == state.value)
        statement = statement.order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
        statement = statement.limit(limit).offset(offset)
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return list(result.scalars())

    async def soft_delete(self, asset_id: str) -> MediaAsset | None:
        now = utc_now()
        statement = (
            update(MediaAsset)
            .where(MediaAsset.id == asset_id, MediaAsset.state != AssetState.DELETED.value)
            .values(
                state=AssetState.DELETED.value,
                deleted_at=now,
                disabled_at=func.coalesce(MediaAsset.disabled_at, now),
                updated_at=now,
            )
            .returning(MediaAsset)
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(statement)
                updated = result.scalar_one_or_none()
                if updated:
                    return updated
                return await session.get(MediaAsset, asset_id)

    async def mark_missing(self, asset_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(MediaAsset)
                    .where(MediaAsset.id == asset_id)
                    .values(state=AssetState.MISSING.value, updated_at=utc_now())
                )

    async def reference_count(self, asset_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(MediaJobAsset).where(
                    MediaJobAsset.asset_id == asset_id
                )
            )
            return int(result.scalar_one())

    async def has_output_relations(self, job_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(MediaJobAsset)
                .where(
                    MediaJobAsset.job_id == job_id,
                    MediaJobAsset.direction == AssetDirection.OUTPUT.value,
                )
            )
            return bool(result.scalar_one())

    async def input_assets_for_job(self, job_id: str) -> list[MediaAsset]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MediaAsset)
                .join(MediaJobAsset, MediaJobAsset.asset_id == MediaAsset.id)
                .where(
                    MediaJobAsset.job_id == job_id,
                    MediaJobAsset.direction == AssetDirection.INPUT.value,
                )
                .order_by(MediaJobAsset.position)
            )
            return list(result.scalars())

    async def output_assets_for_job(self, job_id: str) -> list[tuple[MediaJobAsset, MediaAsset]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MediaJobAsset, MediaAsset)
                .join(MediaAsset, MediaAsset.id == MediaJobAsset.asset_id)
                .where(
                    MediaJobAsset.job_id == job_id,
                    MediaJobAsset.direction == AssetDirection.OUTPUT.value,
                )
                .order_by(MediaJobAsset.position)
            )
            return list(result.tuples())

    async def claim_cleanup(self, asset_id: str) -> MediaAsset:
        """Atomically make an unreferenced asset unavailable and acquire cleanup rights."""

        now = utc_now()
        relation_exists = exists(
            select(MediaJobAsset.asset_id).where(MediaJobAsset.asset_id == asset_id)
        )
        statement = (
            update(MediaAsset)
            .where(
                MediaAsset.id == asset_id,
                MediaAsset.state.in_(
                    (
                        AssetState.AVAILABLE.value,
                        AssetState.DELETED.value,
                        AssetState.MISSING.value,
                    )
                ),
                ~relation_exists,
            )
            .values(
                state=AssetState.DISABLED.value,
                disabled_at=func.coalesce(MediaAsset.disabled_at, now),
                updated_at=now,
            )
            .returning(MediaAsset)
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(statement)
                claimed = result.scalar_one_or_none()
                if claimed is not None:
                    return claimed
                asset = await session.get(MediaAsset, asset_id)
                if asset is None:
                    raise LookupError("asset not found")
                if asset.state == AssetState.DISABLED.value:
                    raise RuntimeError("asset cleanup is already claimed")
                raise AssetReferencedError

    async def purge_claimed_record(self, asset_id: str) -> None:
        relation_exists = exists(
            select(MediaJobAsset.asset_id).where(MediaJobAsset.asset_id == asset_id)
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(MediaAsset).where(
                        MediaAsset.id == asset_id,
                        MediaAsset.state == AssetState.DISABLED.value,
                        ~relation_exists,
                    )
                )
                if result.rowcount != 1:
                    asset = await session.get(MediaAsset, asset_id)
                    if asset is None:
                        return
                    raise AssetReferencedError

    async def all_assets(self) -> list[MediaAsset]:
        async with self._session_factory() as session:
            result = await session.execute(select(MediaAsset))
            return list(result.scalars())

    async def succeeded_jobs_without_outputs(self) -> list[str]:
        relation = (
            select(MediaJobAsset.job_id)
            .where(MediaJobAsset.direction == AssetDirection.OUTPUT.value)
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(MediaJob.job_id).where(
                    MediaJob.status == "succeeded", MediaJob.job_id.not_in(relation)
                )
            )
            return list(result.scalars())

    async def unavailable_output_relations(self) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MediaJobAsset.job_id, MediaJobAsset.asset_id)
                .join(MediaAsset, MediaAsset.id == MediaJobAsset.asset_id)
                .where(
                    MediaJobAsset.direction == AssetDirection.OUTPUT.value,
                    MediaAsset.state != AssetState.AVAILABLE.value,
                )
            )
            return [f"{job_id}:{asset_id}" for job_id, asset_id in result]

    async def register_output_group(
        self,
        *,
        job_id: str,
        lease_owner: str,
        expected_version: int,
        assets: list[MediaAsset],
        roles: list[str],
        output_metadata: list[dict],
    ) -> MediaJob:
        """Register all outputs and compatibility projection under the owned lease."""

        asset_ids = [asset.id for asset in assets]
        session = self._session_factory()
        transaction = await session.begin()
        try:
            try:
                job = await session.get(MediaJob, job_id)
                if (
                    job is None
                    or job.status != "running"
                    or job.version != expected_version
                    or job.lease_owner != lease_owner
                ):
                    from pixelle_video.media_jobs.repository import CASConflictError

                    raise CASConflictError("media job lease was lost")
                for position, asset in enumerate(assets):
                    if (
                        asset.kind != AssetKind.OUTPUT.value
                        or asset.state != AssetState.AVAILABLE.value
                    ):
                        raise ValueError("output asset is unavailable")
                    self._stage_output_asset(session, asset)
                    self._stage_output_relation(
                        session,
                        MediaJobAsset(
                            job_id=job_id,
                            asset_id=asset.id,
                            direction=AssetDirection.OUTPUT.value,
                            role=roles[position],
                            position=position,
                        ),
                    )
                self._project_output_metadata(job, output_metadata)
                job.updated_at = utc_now()
                job.version += 1
                await self._flush_output_registration(session)
            except Exception as error:
                await transaction.rollback()
                from pixelle_video.media_jobs.repository import CASConflictError

                if isinstance(error, CASConflictError):
                    raise
                raise OutputRegistrationError(
                    OutputRegistrationDisposition.NOT_COMMITTED
                ) from error
            try:
                await self._commit_output_registration(transaction)
                return job
            except Exception as error:
                await session.rollback()
                disposition = await self._classify_output_registration(
                    job_id=job_id,
                    asset_ids=asset_ids,
                    output_metadata=output_metadata,
                )
                if disposition is OutputRegistrationDisposition.COMMITTED:
                    persisted = await self.get_job(job_id)
                    assert persisted is not None
                    return persisted
                raise OutputRegistrationError(disposition) from error
        finally:
            await session.close()

    @staticmethod
    def _stage_output_asset(session: AsyncSession, asset: MediaAsset) -> None:
        session.add(asset)

    @staticmethod
    def _stage_output_relation(session: AsyncSession, relation: MediaJobAsset) -> None:
        session.add(relation)

    @staticmethod
    def _project_output_metadata(job: MediaJob, output_metadata: list[dict]) -> None:
        job.output_metadata = output_metadata

    @staticmethod
    async def _flush_output_registration(session: AsyncSession) -> None:
        await session.flush()

    @staticmethod
    async def _commit_output_registration(transaction) -> None:
        await transaction.commit()

    async def get_job(self, job_id: str) -> MediaJob | None:
        async with self._session_factory() as session:
            return await session.get(MediaJob, job_id)

    async def get_output_schema(self, workflow_type: str) -> OutputSchema | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OutputSchema).where(OutputSchema.workflow_type == workflow_type)
            )
            return result.scalar_one_or_none()

    async def list_output_schemas(self) -> list[OutputSchema]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OutputSchema).order_by(OutputSchema.workflow_type.asc())
            )
            return list(result.scalars())

    async def _classify_output_registration(
        self,
        *,
        job_id: str,
        asset_ids: list[str],
        output_metadata: list[dict],
    ) -> OutputRegistrationDisposition:
        async with self._session_factory() as session:
            assets = set(
                (
                    await session.execute(
                        select(MediaAsset.id).where(MediaAsset.id.in_(asset_ids))
                    )
                ).scalars()
            )
            relations = set(
                (
                    await session.execute(
                        select(MediaJobAsset.asset_id).where(
                            MediaJobAsset.job_id == job_id,
                            MediaJobAsset.direction == AssetDirection.OUTPUT.value,
                            MediaJobAsset.asset_id.in_(asset_ids),
                        )
                    )
                ).scalars()
            )
            job = await session.get(MediaJob, job_id)
        expected = set(asset_ids)
        if (
            assets == expected
            and relations == expected
            and job is not None
            and job.output_metadata == output_metadata
        ):
            return OutputRegistrationDisposition.COMMITTED
        if not assets and not relations:
            return OutputRegistrationDisposition.NOT_COMMITTED
        return OutputRegistrationDisposition.UNKNOWN
