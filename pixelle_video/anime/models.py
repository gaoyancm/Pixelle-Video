"""SQLAlchemy persistence models for the phase 07 anime pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now

# moyin-creator 6-layer identity anchors (C1)
ANCHOR_LAYERS = (
    "bone_structure",
    "facial_features",
    "unique_marks",
    "color_palette",
    "texture",
    "hair",
)


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role_type: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    identity_anchors_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    static_features_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dynamic_features_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reference_images_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    voice_config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class SceneAsset(Base):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    environment_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reference_images_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class Prop(Base):
    __tablename__ = "props"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    reference_images_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class AnimeProject(Base):
    __tablename__ = "anime_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)
    world_setting: Mapped[str | None] = mapped_column(Text)
    style_profile: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'producing', 'completed')",
            name="ck_episodes_status",
        ),
        UniqueConstraint("anime_project_id", "season_no", "episode_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    anime_project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("anime_projects.id", ondelete="CASCADE")
    )
    season_no: Mapped[int] = mapped_column(Integer, nullable=False)
    episode_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    script_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class SceneInEpisode(Base):
    __tablename__ = "scenes_in_episode"
    __table_args__ = (UniqueConstraint("episode_id", "scene_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    episode_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="CASCADE")
    )
    scene_no: Mapped[int] = mapped_column(Integer, nullable=False)
    location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    characters_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )


class Shot(Base):
    __tablename__ = "shots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'queued', 'succeeded', 'failed', 'rejected', 'done')",
            name="ck_shots_status",
        ),
        UniqueConstraint("scene_id", "shot_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes_in_episode.id", ondelete="CASCADE")
    )
    shot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    camera_setup_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    visual_description: Mapped[str] = mapped_column(Text, nullable=False)
    character_states_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    image_prompt: Mapped[str | None] = mapped_column(Text)
    video_prompt: Mapped[str | None] = mapped_column(Text)
    parent_shot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("shots.id", ondelete="SET NULL")
    )
    reference_shot_ids_json: Mapped[list[str] | None] = mapped_column(JSON)
    generated_asset_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
