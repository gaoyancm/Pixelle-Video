"""SQLAlchemy persistence models for the phase 04-B QC pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now

QC_CATEGORIES = ("technical", "visual", "character", "narrative", "brand", "platform")
QC_RULE_TYPES = ("schema_validation", "parameter_check", "text_analysis")


class QCRule(Base):
    """One configurable QC check rule."""

    __tablename__ = "qc_rules"
    __table_args__ = (
        CheckConstraint(
            "category IN ('technical', 'visual', 'character', 'narrative', 'brand', 'platform')",
            name="ck_qc_rules_category",
        ),
        CheckConstraint(
            "rule_type IN ('schema_validation', 'parameter_check', 'text_analysis')",
            name="ck_qc_rules_rule_type",
        ),
        CheckConstraint("is_active IN (0, 1)", name="ck_qc_rules_is_active"),
        CheckConstraint("priority >= 1", name="ck_qc_rules_priority"),
        UniqueConstraint("name", name="uq_qc_rules_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local", server_default=text("'local'")
    )
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
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


class QCProfile(Base):
    """A named collection of QC rules with optional severity overrides."""

    __tablename__ = "qc_profiles"
    __table_args__ = (
        CheckConstraint("is_default IN (0, 1)", name="ck_qc_profiles_is_default"),
        UniqueConstraint("name", name="uq_qc_profiles_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rules_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    is_default: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
