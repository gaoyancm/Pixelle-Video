"""SQLAlchemy persistence models for the phase 04-A prompt template system."""

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


class PromptTemplate(Base):
    """One reusable prompt template with Mustache-style variables."""

    __tablename__ = "prompt_templates"
    __table_args__ = (
        CheckConstraint("is_active IN (0, 1)", name="ck_prompt_templates_is_active"),
        CheckConstraint(
            "current_score IS NULL OR current_score BETWEEN 1 AND 5",
            name="ck_prompt_templates_current_score",
        ),
        UniqueConstraint("name", name="uq_prompt_templates_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    variables_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="default", server_default=text("'default'")
    )
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    current_score: Mapped[int | None] = mapped_column(Integer)
    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
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


class PromptVersion(Base):
    """Immutable snapshot of a template for one version number."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        CheckConstraint("version_no >= 1", name="ck_prompt_versions_version_no"),
        UniqueConstraint(
            "template_id",
            "version_no",
            name="uq_prompt_versions_template_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prompt_templates.id", ondelete="RESTRICT"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    variables_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class PromptTag(Base):
    """A reusable tag name for prompt discovery."""

    __tablename__ = "prompt_tags"
    __table_args__ = (UniqueConstraint("name", name="uq_prompt_tags_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class PromptTemplateTag(Base):
    """Many-to-many binding between templates and tags."""

    __tablename__ = "prompt_template_tags"

    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prompt_templates.id", ondelete="RESTRICT"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prompt_tags.id", ondelete="RESTRICT"), primary_key=True
    )
