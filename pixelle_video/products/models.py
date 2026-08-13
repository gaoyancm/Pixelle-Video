"""SQLAlchemy persistence models for the phase 05 product briefs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now

BRIEF_STATUSES = ("draft", "submitted", "processing", "completed", "archived")
SUPPORTED_PLATFORMS = ("etsy", "tiktok", "instagram", "meta", "youtube_shorts")


class ProductBrief(Base):
    """Structured product information feeding the ad production pipeline."""

    __tablename__ = "product_briefs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'submitted', 'processing', 'completed', 'archived')",
            name="ck_product_briefs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL")
    )
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    selling_points_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    target_audience: Mapped[str | None] = mapped_column(String(255))
    brand_profile_id: Mapped[str | None] = mapped_column(String(36))
    platforms_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    reference_images_json: Mapped[list[str] | None] = mapped_column(JSON)
    plan_id: Mapped[str | None] = mapped_column(String(36))
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
