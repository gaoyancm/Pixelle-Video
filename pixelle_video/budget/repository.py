"""Short-transaction repository for the phase 03-F global budget configuration."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pixelle_video.media_jobs.models import utc_now

from .models import BudgetConfig

_GLOBAL_ROW_ID = "global"


class BudgetRepository:
    """Persist the single global budget row."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def get_config(self) -> BudgetConfig:
        async with self._session_factory() as session:
            row = await session.get(BudgetConfig, _GLOBAL_ROW_ID)
            if row is not None:
                return row
            row = BudgetConfig(
                id=_GLOBAL_ROW_ID,
                per_task_limit=None,
                per_batch_limit=None,
                mode="observe",
            )
            session.add(row)
            await session.flush()
            return row

    async def update_config(
        self,
        *,
        per_task_limit: float | None,
        per_batch_limit: float | None,
        mode: str,
    ) -> BudgetConfig:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(BudgetConfig, _GLOBAL_ROW_ID)
                if row is None:
                    row = BudgetConfig(
                        id=_GLOBAL_ROW_ID,
                        per_task_limit=per_task_limit,
                        per_batch_limit=per_batch_limit,
                        mode=mode,
                    )
                    session.add(row)
                else:
                    row.per_task_limit = per_task_limit
                    row.per_batch_limit = per_batch_limit
                    row.mode = mode
                    row.updated_at = utc_now()
                await session.flush()
                return row
