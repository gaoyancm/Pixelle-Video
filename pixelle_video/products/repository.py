"""Short-transaction repository for phase 05 product briefs."""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import ProductBrief


class ProductBriefNotFoundError(RuntimeError):
    pass


class ProductBriefRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_brief(
        self,
        *,
        product_name: str,
        description: str,
        project_id: str | None = None,
        category: str | None = None,
        selling_points: Sequence[str] | None = None,
        target_audience: str | None = None,
        brand_profile_id: str | None = None,
        platforms: Sequence[str] | None = None,
        reference_images: Sequence[str] | None = None,
        plan_id: str | None = None,
        status: str = "draft",
        brief_id: str | None = None,
    ) -> ProductBrief:
        brief = ProductBrief(
            id=brief_id or str(uuid.uuid4()),
            project_id=project_id,
            product_name=product_name,
            category=category,
            description=description,
            selling_points_json=list(selling_points or []),
            target_audience=target_audience,
            brand_profile_id=brand_profile_id,
            platforms_json=list(platforms or []),
            reference_images_json=list(reference_images) if reference_images else None,
            plan_id=plan_id,
            status=status,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(brief)
                await session.flush()
                return brief

    async def get_brief(self, brief_id: str) -> ProductBrief | None:
        async with self._session_factory() as session:
            return await session.get(ProductBrief, brief_id)

    async def list_briefs(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[ProductBrief], bool]:
        statement = select(ProductBrief).order_by(
            ProductBrief.created_at.desc(), ProductBrief.id.desc()
        )
        if project_id is not None:
            statement = statement.where(ProductBrief.project_id == project_id)
        if status is not None:
            statement = statement.where(ProductBrief.status == status)
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit

    async def update_status(self, brief_id: str, status: str) -> ProductBrief:
        async with self._session_factory() as session:
            async with session.begin():
                brief = await session.get(ProductBrief, brief_id)
                if brief is None:
                    raise ProductBriefNotFoundError("product brief not found")
                brief.status = status
                await session.flush()
                return brief

    async def update_brief(
        self,
        brief_id: str,
        *,
        product_name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        selling_points: Sequence[str] | None = None,
        target_audience: str | None = None,
        platforms: Sequence[str] | None = None,
        status: str | None = None,
    ) -> ProductBrief:
        async with self._session_factory() as session:
            async with session.begin():
                brief = await session.get(ProductBrief, brief_id)
                if brief is None:
                    raise ProductBriefNotFoundError("product brief not found")
                if product_name is not None:
                    brief.product_name = product_name
                if description is not None:
                    brief.description = description
                if category is not None:
                    brief.category = category
                if selling_points is not None:
                    brief.selling_points_json = list(selling_points)
                if target_audience is not None:
                    brief.target_audience = target_audience
                if platforms is not None:
                    brief.platforms_json = list(platforms)
                if status is not None:
                    brief.status = status
                await session.flush()
                return brief
