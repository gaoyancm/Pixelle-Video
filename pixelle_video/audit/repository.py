"""Short-transaction repository for phase 03-F audit events."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.models import utc_now

from .models import AuditEvent


class AuditRepository:
    """Persist and query audit events without framework dependencies."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def record(
        self,
        *,
        event_type: str,
        scope_type: str,
        scope_id: str,
        operator: str = "system",
        details: dict[str, Any] | None = None,
        cost_snapshot: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=event_id or str(uuid.uuid4()),
            event_type=event_type,
            scope_type=scope_type,
            scope_id=scope_id,
            operator=operator,
            details_json=details,
            cost_snapshot=cost_snapshot,
            created_at=utc_now(),
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(event)
                await session.flush()
        return event

    async def get(self, event_id: str) -> AuditEvent | None:
        async with self._session_factory() as session:
            return await session.get(AuditEvent, event_id)

    async def list(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditEvent], bool]:
        """Return a stable, newest-first page and whether more rows exist."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        statement = select(AuditEvent)
        if scope_type is not None:
            statement = statement.where(AuditEvent.scope_type == scope_type)
        if scope_id is not None:
            statement = statement.where(AuditEvent.scope_id == scope_id)
        if event_type is not None:
            statement = statement.where(AuditEvent.event_type == event_type)
        statement = statement.order_by(AuditEvent.created_at.desc(), AuditEvent.event_id.desc())
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit

    async def count_for_scope(self, scope_type: str, scope_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.scope_type == scope_type, AuditEvent.scope_id == scope_id)
            )
            return int(result.scalar_one())
