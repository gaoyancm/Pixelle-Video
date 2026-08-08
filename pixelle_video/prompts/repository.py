"""Short-transaction repository for phase 04-A prompt templates."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.models import utc_now

from .models import PromptTag, PromptTemplate, PromptTemplateTag, PromptVersion


class PromptNotFoundError(RuntimeError):
    pass


class PromptNameConflictError(RuntimeError):
    pass


class PromptVersionNotFoundError(RuntimeError):
    pass


class PromptTagNotFoundError(RuntimeError):
    pass


class PromptRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_template(
        self,
        *,
        name: str,
        category: str,
        template_text: str,
        description: str | None = None,
        variables_json: list[dict[str, Any]] | None = None,
        provider: str = "default",
        is_active: int = 1,
        change_note: str | None = None,
        template_id: str | None = None,
    ) -> PromptTemplate:
        template = PromptTemplate(
            id=template_id or str(uuid.uuid4()),
            name=name,
            category=category,
            description=description,
            template_text=template_text,
            variables_json=variables_json or [],
            provider=provider,
            is_active=is_active,
        )
        version = PromptVersion(
            id=str(uuid.uuid4()),
            template_id=template.id,
            version_no=1,
            template_text=template_text,
            variables_json=template.variables_json,
            change_note=change_note or "初始版本",
        )
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    session.add(template)
                    await session.flush()
                    session.add(version)
                    await session.flush()
            except IntegrityError:
                raise PromptNameConflictError(
                    f"prompt template name already exists: {name}"
                ) from None
        return template

    async def get_template(self, template_id: str) -> PromptTemplate | None:
        async with self._session_factory() as session:
            return await session.get(PromptTemplate, template_id)

    async def list_templates(
        self,
        *,
        category: str | None = None,
        is_active: bool | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[PromptTemplate], bool]:
        statement = select(PromptTemplate).where(PromptTemplate.archived_at.is_(None))
        if category is not None:
            statement = statement.where(PromptTemplate.category == category)
        if is_active is not None:
            statement = statement.where(PromptTemplate.is_active == (1 if is_active else 0))
        statement = statement.order_by(PromptTemplate.updated_at.desc(), PromptTemplate.id.desc())
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit

    async def list_categories(self) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PromptTemplate.category)
                .where(PromptTemplate.archived_at.is_(None))
                .distinct()
                .order_by(PromptTemplate.category.asc())
            )
            return list(result.scalars())

    async def update_template(
        self,
        template_id: str,
        *,
        name: str | None = None,
        category: str | None = None,
        description: str | None = None,
        template_text: str | None = None,
        variables_json: list[dict[str, Any]] | None = None,
        provider: str | None = None,
        is_active: int | None = None,
        change_note: str | None = None,
    ) -> PromptTemplate:
        """Update a template and append a new immutable version (P3)."""
        now = utc_now()
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    template = await session.get(PromptTemplate, template_id)
                    if template is None:
                        raise PromptNotFoundError("prompt template not found")
                    if template.archived_at is not None:
                        raise PromptNotFoundError("archived template cannot be updated")
                    if name is not None:
                        template.name = name
                    if category is not None:
                        template.category = category
                    if description is not None:
                        template.description = description
                    if template_text is not None:
                        template.template_text = template_text
                    if variables_json is not None:
                        template.variables_json = variables_json
                    if provider is not None:
                        template.provider = provider
                    if is_active is not None:
                        template.is_active = is_active
                    template.updated_at = now
                    # Append the new version snapshot.
                    latest = await self._latest_version_no(session, template_id)
                    session.add(
                        PromptVersion(
                            id=str(uuid.uuid4()),
                            template_id=template_id,
                            version_no=latest + 1,
                            template_text=template.template_text,
                            variables_json=template.variables_json,
                            change_note=change_note or "更新模板",
                        )
                    )
                    await session.flush()
                    return template
            except IntegrityError:
                raise PromptNameConflictError("prompt template name already exists") from None

    async def _latest_version_no(self, session: AsyncSession, template_id: str) -> int:
        result = await session.execute(
            select(func.max(PromptVersion.version_no)).where(
                PromptVersion.template_id == template_id
            )
        )
        return int(result.scalar() or 0)

    async def list_versions(self, template_id: str) -> list[PromptVersion]:
        async with self._session_factory() as session:
            template = await session.get(PromptTemplate, template_id)
            if template is None:
                raise PromptNotFoundError("prompt template not found")
            result = await session.execute(
                select(PromptVersion)
                .where(PromptVersion.template_id == template_id)
                .order_by(PromptVersion.version_no.asc())
            )
            return list(result.scalars())

    async def rollback(
        self, template_id: str, version_no: int, *, change_note: str | None = None
    ) -> PromptTemplate:
        """Restore a template to a historical version (P3)."""
        now = utc_now()
        async with self._session_factory() as session:
            async with session.begin():
                template = await session.get(PromptTemplate, template_id)
                if template is None:
                    raise PromptNotFoundError("prompt template not found")
                if template.archived_at is not None:
                    raise PromptNotFoundError("archived template cannot be rolled back")
                version = await self._get_version(session, template_id, version_no)
                if version is None:
                    raise PromptVersionNotFoundError(f"version {version_no} not found for template")
                latest = await self._latest_version_no(session, template_id)
                template.template_text = version.template_text
                template.variables_json = version.variables_json
                template.updated_at = now
                session.add(
                    PromptVersion(
                        id=str(uuid.uuid4()),
                        template_id=template_id,
                        version_no=latest + 1,
                        template_text=version.template_text,
                        variables_json=version.variables_json,
                        change_note=change_note or f"回滚到版本 {version_no}",
                    )
                )
                await session.flush()
                return template

    async def _get_version(
        self, session: AsyncSession, template_id: str, version_no: int
    ) -> PromptVersion | None:
        result = await session.execute(
            select(PromptVersion).where(
                PromptVersion.template_id == template_id,
                PromptVersion.version_no == version_no,
            )
        )
        return result.scalar_one_or_none()

    async def rate_template(
        self, template_id: str, *, score: int, comment: str | None = None
    ) -> PromptTemplate:
        async with self._session_factory() as session:
            async with session.begin():
                template = await session.get(PromptTemplate, template_id)
                if template is None:
                    raise PromptNotFoundError("prompt template not found")
                if not 1 <= score <= 5:
                    raise ValueError("score must be between 1 and 5")
                template.current_score = score
                template.updated_at = utc_now()
                if comment:
                    latest = await self._latest_version_no(session, template_id)
                    session.add(
                        PromptVersion(
                            id=str(uuid.uuid4()),
                            template_id=template_id,
                            version_no=latest + 1,
                            template_text=template.template_text,
                            variables_json=template.variables_json,
                            change_note=f"评分 {score}：{comment}",
                        )
                    )
                await session.flush()
                return template

    async def record_usage(self, template_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(PromptTemplate)
                    .where(PromptTemplate.id == template_id)
                    .values(
                        usage_count=PromptTemplate.usage_count + 1,
                        last_used_at=utc_now(),
                        updated_at=utc_now(),
                    )
                )
                if result.rowcount == 0:
                    raise PromptNotFoundError("prompt template not found")

    async def archive_template(self, template_id: str) -> PromptTemplate:
        async with self._session_factory() as session:
            async with session.begin():
                template = await session.get(PromptTemplate, template_id)
                if template is None:
                    raise PromptNotFoundError("prompt template not found")
                if template.archived_at is None:
                    template.archived_at = utc_now()
                    template.is_active = 0
                    template.updated_at = utc_now()
                await session.flush()
                return template

    # --- P4 tags ------------------------------------------------------------

    async def list_tags(self) -> list[PromptTag]:
        async with self._session_factory() as session:
            result = await session.execute(select(PromptTag).order_by(PromptTag.name.asc()))
            return list(result.scalars())

    async def get_tag(self, tag_id: str) -> PromptTag | None:
        async with self._session_factory() as session:
            return await session.get(PromptTag, tag_id)

    async def bind_tags(self, template_id: str, tag_ids: Sequence[str]) -> list[PromptTag]:
        async with self._session_factory() as session:
            async with session.begin():
                template = await session.get(PromptTemplate, template_id)
                if template is None:
                    raise PromptNotFoundError("prompt template not found")
                bound: list[PromptTag] = []
                for tag_id in dict.fromkeys(tag_ids):
                    tag = await session.get(PromptTag, tag_id)
                    if tag is None:
                        raise PromptTagNotFoundError(f"tag not found: {tag_id}")
                    exists = await session.get(PromptTemplateTag, (template_id, tag_id))
                    if exists is None:
                        session.add(PromptTemplateTag(template_id=template_id, tag_id=tag_id))
                    bound.append(tag)
                await session.flush()
                return bound

    async def template_tags(self, template_id: str) -> list[PromptTag]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PromptTag)
                .join(PromptTemplateTag, PromptTemplateTag.tag_id == PromptTag.id)
                .where(PromptTemplateTag.template_id == template_id)
                .order_by(PromptTag.name.asc())
            )
            return list(result.scalars())

    async def search(
        self,
        *,
        tag: str | None = None,
        category: str | None = None,
        q: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[PromptTemplate], bool]:
        statement = select(PromptTemplate).where(PromptTemplate.archived_at.is_(None))
        if category is not None:
            statement = statement.where(PromptTemplate.category == category)
        if tag is not None:
            statement = statement.join(
                PromptTemplateTag, PromptTemplateTag.template_id == PromptTemplate.id
            ).join(PromptTag, PromptTag.id == PromptTemplateTag.tag_id)
            statement = statement.where(PromptTag.name == tag)
        if q:
            pattern = f"%{q}%"
            statement = statement.where(
                or_(
                    PromptTemplate.name.like(pattern),
                    PromptTemplate.description.like(pattern),
                    PromptTemplate.template_text.like(pattern),
                )
            )
        statement = statement.order_by(PromptTemplate.updated_at.desc(), PromptTemplate.id.desc())
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit
