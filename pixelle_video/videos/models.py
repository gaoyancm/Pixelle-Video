"""SQLAlchemy persistence models for the phase 06 short-video scripts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now

SCRIPT_STATUSES = (
    "draft",
    "confirmed",
    "storyboarding",
    "assets",
    "composing",
    "completed",
    "archived",
)


class VideoScript(Base):
    """One structured short-video script generated from a topic."""

    __tablename__ = "video_scripts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'storyboarding', 'assets', 'composing', 'completed', 'archived')",
            name="ck_video_scripts_status",
        ),
        CheckConstraint("target_duration > 0", name="ck_video_scripts_target_duration"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL")
    )
    topic: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-CN", server_default=text("'zh-CN'")
    )
    target_duration: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default=text("60")
    )
    platform: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tiktok", server_default=text("'tiktok'")
    )
    script_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    prompt_version_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
