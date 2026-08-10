"""Short-transaction repository for phase 06 video scripts."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import VideoScript


class VideoScriptNotFoundError(RuntimeError):
    pass


class VideoScriptRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_script(
        self,
        *,
        topic: str,
        language: str = "zh-CN",
        target_duration: int = 60,
        platform: str = "tiktok",
        project_id: str | None = None,
        script_json: dict[str, Any] | None = None,
        prompt_version_id: str | None = None,
        status: str = "draft",
        script_id: str | None = None,
    ) -> VideoScript:
        script = VideoScript(
            id=script_id or str(uuid.uuid4()),
            project_id=project_id,
            topic=topic,
            language=language,
            target_duration=target_duration,
            platform=platform,
            script_json=script_json,
            prompt_version_id=prompt_version_id,
            status=status,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(script)
                await session.flush()
                return script

    async def get_script(self, script_id: str) -> VideoScript | None:
        async with self._session_factory() as session:
            return await session.get(VideoScript, script_id)

    async def update_script(
        self,
        script_id: str,
        *,
        script_json: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> VideoScript:
        async with self._session_factory() as session:
            async with session.begin():
                script = await session.get(VideoScript, script_id)
                if script is None:
                    raise VideoScriptNotFoundError("video script not found")
                if script_json is not None:
                    script.script_json = script_json
                if status is not None:
                    script.status = status
                await session.flush()
                return script

    async def update_status(self, script_id: str, status: str) -> VideoScript:
        async with self._session_factory() as session:
            async with session.begin():
                script = await session.get(VideoScript, script_id)
                if script is None:
                    raise VideoScriptNotFoundError("video script not found")
                script.status = status
                await session.flush()
                return script

    async def list_scripts(
        self,
        *,
        project_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[VideoScript], bool]:
        statement = select(VideoScript).order_by(
            VideoScript.created_at.desc(), VideoScript.id.desc()
        )
        if project_id is not None:
            statement = statement.where(VideoScript.project_id == project_id)
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit
