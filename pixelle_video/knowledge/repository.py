"""Short-transaction repository for the phase 04-D knowledge base."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    STALE_AFTER_DAYS,
    KnowledgeEntry,
    KnowledgeEntryTag,
    KnowledgeLink,
    KnowledgeTag,
)


class KnowledgeEntryNotFoundError(RuntimeError):
    pass


class KnowledgeValidationError(RuntimeError):
    """Raised when an entry violates the K2 source-tracing rules."""


class KnowledgeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    # --- K1 CRUD ----------------------------------------------------------------

    async def create_entry(
        self,
        *,
        title: str,
        content: str,
        category: str,
        evidence_class: str,
        status: str = "draft",
        source_url: str | None = None,
        source_doc: str | None = None,
        tags: Sequence[str] | None = None,
        entry_id: str | None = None,
    ) -> KnowledgeEntry:
        _require_source(evidence_class, source_url, source_doc)
        entry = KnowledgeEntry(
            id=entry_id or str(uuid.uuid4()),
            title=title,
            content=content,
            category=category,
            evidence_class=evidence_class,
            status=status,
            source_url=source_url,
            source_doc=source_doc,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entry)
                await session.flush()
                if tags:
                    await self._bind_tags(session, entry.id, tags)
                await session.flush()
        return entry

    async def get_entry(self, entry_id: str) -> KnowledgeEntry | None:
        async with self._session_factory() as session:
            return await session.get(KnowledgeEntry, entry_id)

    async def list_entries(
        self,
        *,
        category: str | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[KnowledgeEntry], bool]:
        statement = select(KnowledgeEntry).order_by(
            KnowledgeEntry.updated_at.desc(), KnowledgeEntry.id.desc()
        )
        if category is not None:
            statement = statement.where(KnowledgeEntry.category == category)
        if status is not None:
            statement = statement.where(KnowledgeEntry.status == status)
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit

    async def update_entry(
        self,
        entry_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
        evidence_class: str | None = None,
        status: str | None = None,
        source_url: str | None = None,
        source_doc: str | None = None,
    ) -> KnowledgeEntry:
        async with self._session_factory() as session:
            async with session.begin():
                entry = await session.get(KnowledgeEntry, entry_id)
                if entry is None:
                    raise KnowledgeEntryNotFoundError("knowledge entry not found")
                if title is not None:
                    entry.title = title
                if content is not None:
                    entry.content = content
                if category is not None:
                    entry.category = category
                if evidence_class is not None:
                    entry.evidence_class = evidence_class
                if status is not None:
                    entry.status = status
                if source_url is not None:
                    entry.source_url = source_url
                if source_doc is not None:
                    entry.source_doc = source_doc
                _require_source(entry.evidence_class, entry.source_url, entry.source_doc)
                await session.flush()
                return entry

    async def archive_entry(self, entry_id: str) -> KnowledgeEntry:
        async with self._session_factory() as session:
            async with session.begin():
                entry = await session.get(KnowledgeEntry, entry_id)
                if entry is None:
                    raise KnowledgeEntryNotFoundError("knowledge entry not found")
                entry.status = "archived"
                await session.flush()
                return entry

    async def mark_verified(
        self, entry_id: str, verified_at: datetime | None = None
    ) -> KnowledgeEntry:
        async with self._session_factory() as session:
            async with session.begin():
                entry = await session.get(KnowledgeEntry, entry_id)
                if entry is None:
                    raise KnowledgeEntryNotFoundError("knowledge entry not found")
                entry.verified_at = verified_at or datetime.now(timezone.utc)
                await session.flush()
                return entry

    async def list_stale(self, now: datetime | None = None) -> list[KnowledgeEntry]:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=STALE_AFTER_DAYS)
        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.status != "archived",
                    or_(
                        KnowledgeEntry.verified_at.is_(None),
                        KnowledgeEntry.verified_at < cutoff,
                    ),
                )
            )
            return list(result.scalars())

    @staticmethod
    def is_stale(entry: KnowledgeEntry, now: datetime | None = None) -> bool:
        verified_at = entry.verified_at
        if verified_at is None:
            return entry.status != "archived"
        # SQLite returns naive datetimes; normalize to naive UTC for comparison.
        if verified_at.tzinfo is not None:
            verified_at = verified_at.astimezone(timezone.utc).replace(tzinfo=None)
        base = now or datetime.now(timezone.utc)
        if base.tzinfo is not None:
            base = base.replace(tzinfo=None)
        cutoff = base - timedelta(days=STALE_AFTER_DAYS)
        return verified_at < cutoff and entry.status != "archived"

    # --- tags -------------------------------------------------------------------

    async def _bind_tags(self, session: AsyncSession, entry_id: str, tags: Sequence[str]) -> None:
        for name in tags:
            result = await session.execute(select(KnowledgeTag).where(KnowledgeTag.name == name))
            tag = result.scalar_one_or_none()
            if tag is None:
                tag = KnowledgeTag(id=str(uuid.uuid4()), name=name)
                session.add(tag)
                await session.flush()
            session.add(KnowledgeEntryTag(entry_id=entry_id, tag_id=tag.id))

    async def list_tags(self) -> list[KnowledgeTag]:
        async with self._session_factory() as session:
            result = await session.execute(select(KnowledgeTag).order_by(KnowledgeTag.name.asc()))
            return list(result.scalars())

    async def tags_for_entry(self, entry_id: str) -> list[KnowledgeTag]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeTag)
                .join(KnowledgeEntryTag, KnowledgeEntryTag.tag_id == KnowledgeTag.id)
                .where(KnowledgeEntryTag.entry_id == entry_id)
                .order_by(KnowledgeTag.name.asc())
            )
            return list(result.scalars())

    # --- K3 links & search --------------------------------------------------------

    async def add_link(
        self,
        entry_id: str,
        *,
        target_type: str,
        target_id: str,
        link_note: str | None = None,
    ) -> KnowledgeLink:
        async with self._session_factory() as session:
            async with session.begin():
                entry = await session.get(KnowledgeEntry, entry_id)
                if entry is None:
                    raise KnowledgeEntryNotFoundError("knowledge entry not found")
                link = KnowledgeLink(
                    id=str(uuid.uuid4()),
                    entry_id=entry_id,
                    target_type=target_type,
                    target_id=target_id,
                    link_note=link_note,
                )
                session.add(link)
                await session.flush()
                return link

    async def list_links(self, entry_id: str) -> list[KnowledgeLink]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeLink)
                .where(KnowledgeLink.entry_id == entry_id)
                .order_by(KnowledgeLink.created_at.desc())
            )
            return list(result.scalars())

    async def search(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        category: str | None = None,
        status: str = "published",
        limit: int,
        offset: int,
    ) -> tuple[list[KnowledgeEntry], bool]:
        statement = (
            select(KnowledgeEntry)
            .join(
                KnowledgeEntryTag,
                KnowledgeEntryTag.entry_id == KnowledgeEntry.id,
                isouter=True,
            )
            .join(
                KnowledgeTag,
                KnowledgeTag.id == KnowledgeEntryTag.tag_id,
                isouter=True,
            )
            .where(KnowledgeEntry.status == status)
        )
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    KnowledgeEntry.title.like(pattern),
                    KnowledgeEntry.content.like(pattern),
                )
            )
        if tag:
            statement = statement.where(KnowledgeTag.name == tag)
        if category:
            statement = statement.where(KnowledgeEntry.category == category)
        statement = statement.distinct().order_by(
            KnowledgeEntry.updated_at.desc(), KnowledgeEntry.id.desc()
        )
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit


def _require_source(evidence_class: str, source_url: str | None, source_doc: str | None) -> None:
    if evidence_class == "documented_fact" and not (source_url or source_doc):
        raise KnowledgeValidationError("documented_fact 类条目必须提供 source_url 或 source_doc")
