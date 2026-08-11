"""Short-transaction repository for the phase 07 anime pipeline."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    AnimeProject,
    Character,
    Episode,
    Prop,
    SceneAsset,
    SceneInEpisode,
    Shot,
)


class AnimeNotFoundError(RuntimeError):
    pass


class AnimeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    # --- C1 assets ----------------------------------------------------------------

    async def create_character(
        self,
        *,
        name: str,
        description: str,
        identity_anchors: dict[str, Any],
        static_features: dict[str, Any],
        project_id: str | None = None,
        role_type: str | None = None,
        dynamic_features: dict[str, Any] | None = None,
        reference_images: Sequence[dict[str, Any]] | None = None,
        voice_config: dict[str, Any] | None = None,
        entity_id: str | None = None,
    ) -> Character:
        entity = Character(
            id=entity_id or str(uuid.uuid4()),
            project_id=project_id,
            name=name,
            role_type=role_type,
            description=description,
            identity_anchors_json=identity_anchors,
            static_features_json=static_features,
            dynamic_features_json=dynamic_features,
            reference_images_json=list(reference_images or []),
            voice_config_json=voice_config,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    async def get_character(self, character_id: str) -> Character | None:
        async with self._session_factory() as session:
            return await session.get(Character, character_id)

    async def update_character(
        self,
        character_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        identity_anchors: dict[str, Any] | None = None,
        static_features: dict[str, Any] | None = None,
        dynamic_features: dict[str, Any] | None = None,
        reference_images: Sequence[dict[str, Any]] | None = None,
    ) -> Character:
        async with self._session_factory() as session:
            async with session.begin():
                entity = await session.get(Character, character_id)
                if entity is None:
                    raise AnimeNotFoundError("character not found")
                if name is not None:
                    entity.name = name
                if description is not None:
                    entity.description = description
                if identity_anchors is not None:
                    entity.identity_anchors_json = identity_anchors
                if static_features is not None:
                    entity.static_features_json = static_features
                if dynamic_features is not None:
                    entity.dynamic_features_json = dynamic_features
                if reference_images is not None:
                    entity.reference_images_json = list(reference_images)
                await session.flush()
                return entity

    async def list_characters(self, project_id: str | None, limit: int, offset: int):
        statement = select(Character).order_by(Character.created_at.desc(), Character.id.desc())
        if project_id is not None:
            statement = statement.where(Character.project_id == project_id)
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(statement.limit(limit + 1).offset(offset))).scalars()
            )
        return rows[:limit], len(rows) > limit

    async def create_scene_asset(
        self,
        *,
        name: str,
        description: str,
        project_id: str | None = None,
        environment: dict[str, Any] | None = None,
        reference_images: Sequence[dict[str, Any]] | None = None,
        entity_id: str | None = None,
    ) -> SceneAsset:
        entity = SceneAsset(
            id=entity_id or str(uuid.uuid4()),
            project_id=project_id,
            name=name,
            description=description,
            environment_json=environment,
            reference_images_json=list(reference_images or []),
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    async def get_scene_asset(self, scene_id: str) -> SceneAsset | None:
        async with self._session_factory() as session:
            return await session.get(SceneAsset, scene_id)

    async def create_prop(
        self,
        *,
        name: str,
        project_id: str | None = None,
        description: str | None = None,
        reference_images: Sequence[dict[str, Any]] | None = None,
        entity_id: str | None = None,
    ) -> Prop:
        entity = Prop(
            id=entity_id or str(uuid.uuid4()),
            project_id=project_id,
            name=name,
            description=description,
            reference_images_json=list(reference_images or []),
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    # --- C2 series structure -------------------------------------------------------

    async def create_anime_project(
        self,
        *,
        project_id: str,
        world_setting: str | None = None,
        style_profile: str | None = None,
        entity_id: str | None = None,
    ) -> AnimeProject:
        entity = AnimeProject(
            id=entity_id or str(uuid.uuid4()),
            project_id=project_id,
            world_setting=world_setting,
            style_profile=style_profile,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    async def get_anime_project(self, anime_project_id: str) -> AnimeProject | None:
        async with self._session_factory() as session:
            return await session.get(AnimeProject, anime_project_id)

    async def create_episode(
        self,
        *,
        anime_project_id: str,
        season_no: int,
        episode_no: int,
        title: str | None = None,
        script_summary: str | None = None,
        entity_id: str | None = None,
    ) -> Episode:
        entity = Episode(
            id=entity_id or str(uuid.uuid4()),
            anime_project_id=anime_project_id,
            season_no=season_no,
            episode_no=episode_no,
            title=title,
            script_summary=script_summary,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    async def get_episode(self, episode_id: str) -> Episode | None:
        async with self._session_factory() as session:
            return await session.get(Episode, episode_id)

    async def create_scene_in_episode(
        self,
        *,
        episode_id: str,
        scene_no: int,
        description: str | None = None,
        location_id: str | None = None,
        characters: Sequence[dict[str, Any]] | None = None,
        entity_id: str | None = None,
    ) -> SceneInEpisode:
        entity = SceneInEpisode(
            id=entity_id or str(uuid.uuid4()),
            episode_id=episode_id,
            scene_no=scene_no,
            location_id=location_id,
            description=description,
            characters_json=list(characters or []),
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    async def get_scene_in_episode(self, scene_id: str) -> SceneInEpisode | None:
        async with self._session_factory() as session:
            return await session.get(SceneInEpisode, scene_id)

    async def create_shot(
        self,
        *,
        scene_id: str,
        shot_no: int,
        duration_sec: int,
        visual_description: str,
        camera_setup: dict[str, Any] | None = None,
        character_states: Sequence[dict[str, Any]] | None = None,
        parent_shot_id: str | None = None,
        reference_shot_ids: Sequence[str] | None = None,
        entity_id: str | None = None,
    ) -> Shot:
        entity = Shot(
            id=entity_id or str(uuid.uuid4()),
            scene_id=scene_id,
            shot_no=shot_no,
            duration_sec=duration_sec,
            camera_setup_json=camera_setup,
            visual_description=visual_description,
            character_states_json=list(character_states or []),
            parent_shot_id=parent_shot_id,
            reference_shot_ids_json=list(reference_shot_ids) if reference_shot_ids else None,
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(entity)
                await session.flush()
                return entity

    async def get_shot(self, shot_id: str) -> Shot | None:
        async with self._session_factory() as session:
            return await session.get(Shot, shot_id)

    async def list_shots_for_character(self, character_id: str) -> list[Shot]:
        """All shots whose character_states mention the character id."""
        statement = (
            select(Shot)
            .where(Shot.character_states_json.like(f'%"{character_id}"%'))
            .order_by(Shot.shot_no)
        )
        async with self._session_factory() as session:
            return list((await session.execute(statement)).scalars())

    async def list_shots(self, scene_id: str) -> list[Shot]:
        statement = select(Shot).where(Shot.scene_id == scene_id).order_by(Shot.shot_no)
        async with self._session_factory() as session:
            return list((await session.execute(statement)).scalars())

    async def update_shot(
        self,
        shot_id: str,
        *,
        image_prompt: str | None = None,
        video_prompt: str | None = None,
        status: str | None = None,
        generated_asset_id: str | None = None,
    ) -> Shot:
        async with self._session_factory() as session:
            async with session.begin():
                entity = await session.get(Shot, shot_id)
                if entity is None:
                    raise AnimeNotFoundError("shot not found")
                if image_prompt is not None:
                    entity.image_prompt = image_prompt
                if video_prompt is not None:
                    entity.video_prompt = video_prompt
                if status is not None:
                    entity.status = status
                if generated_asset_id is not None:
                    entity.generated_asset_id = generated_asset_id
                await session.flush()
                return entity

    async def list_scenes_in_episode(self, episode_id: str) -> list[SceneInEpisode]:
        statement = (
            select(SceneInEpisode)
            .where(SceneInEpisode.episode_id == episode_id)
            .order_by(SceneInEpisode.scene_no)
        )
        async with self._session_factory() as session:
            return list((await session.execute(statement)).scalars())
