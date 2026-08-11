"""SQLAlchemy persistence models for the phase 04-E LLM orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now

PLAN_STATUSES = (
    "draft",
    "generating",
    "awaiting_approval",
    "approved",
    "rejected",
    "completed",
    "stage_failed",
)
INTENTS = ("product_ad", "short_video", "animation", "unknown")


class ContentPlan(Base):
    """One user request parsed into a structured, orchestrated content plan."""

    __tablename__ = "content_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'generating', 'awaiting_approval', 'approved', 'rejected', 'completed', 'stage_failed')",
            name="ck_content_plans_status",
        ),
        CheckConstraint(
            "intent IN ('product_ad', 'short_video', 'animation', 'unknown')",
            name="ck_content_plans_intent",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL")
    )
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default=text("'draft'")
    )
    cost_estimate: Mapped[float | None] = mapped_column(Float)
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
