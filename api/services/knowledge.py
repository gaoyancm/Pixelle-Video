"""Application service for the phase 04-D knowledge base."""

from __future__ import annotations

from typing import Any

from pixelle_video.audit import AuditRepository
from pixelle_video.knowledge.repository import KnowledgeRepository


class KnowledgeApplicationService:
    """Own knowledge base business rules and audit tracing."""

    def __init__(self, repository: KnowledgeRepository, audit: AuditRepository | None = None):
        self.repository = repository
        self.audit = audit

    # --- K1 ---------------------------------------------------------------------

    async def create(self, body) -> Any:
        entry = await self.repository.create_entry(
            title=body.title,
            content=body.content,
            category=body.category,
            evidence_class=body.evidence_class,
            status=body.status,
            source_url=body.source_url,
            source_doc=body.source_doc,
            tags=body.tags,
        )
        await self._record_change("knowledge_created", entry.id, {"title": entry.title})
        return entry

    async def list(self, category: str | None, status: str | None, limit: int, offset: int):
        rows, has_more = await self.repository.list_entries(
            category=category, status=status, limit=limit, offset=offset
        )
        return rows, has_more

    async def get(self, entry_id: str):
        return await self.repository.get_entry(entry_id)

    async def update(self, entry_id: str, body) -> Any:
        entry = await self.repository.update_entry(
            entry_id,
            title=body.title,
            content=body.content,
            category=body.category,
            evidence_class=body.evidence_class,
            status=body.status,
            source_url=body.source_url,
            source_doc=body.source_doc,
        )
        await self._record_change("knowledge_updated", entry_id, {"title": entry.title})
        return entry

    async def archive(self, entry_id: str) -> Any:
        entry = await self.repository.archive_entry(entry_id)
        await self._record_change("knowledge_archived", entry_id, {"title": entry.title})
        return entry

    # --- K2 ---------------------------------------------------------------------

    async def verify(self, entry_id: str) -> Any:
        entry = await self.repository.mark_verified(entry_id)
        await self._record_change("knowledge_verified", entry_id, {"title": entry.title})
        return entry

    async def stale(self):
        return await self.repository.list_stale()

    # --- K3 ---------------------------------------------------------------------

    async def add_link(self, entry_id: str, body) -> Any:
        return await self.repository.add_link(
            entry_id,
            target_type=body.target_type,
            target_id=body.target_id,
            link_note=body.link_note,
        )

    async def links(self, entry_id: str):
        return await self.repository.list_links(entry_id)

    async def search(
        self, query: str | None, tag: str | None, category: str | None, limit: int, offset: int
    ):
        return await self.repository.search(
            query=query, tag=tag, category=category, status="published", limit=limit, offset=offset
        )

    async def tags(self, entry_id: str):
        return await self.repository.tags_for_entry(entry_id)

    # --- audit --------------------------------------------------------------------

    async def _record_change(self, event_type: str, scope_id: str, details: dict | None) -> None:
        if self.audit is None:
            return
        try:
            await self.audit.record(
                event_type=event_type,
                scope_type="knowledge_entry",
                scope_id=scope_id,
                details=details,
            )
        except Exception:
            pass
