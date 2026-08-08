"""SQLAlchemy persistence model for the phase 03-F global budget configuration."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Float, String, text
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now


class BudgetConfig(Base):
    """One global budget row; mode is observe | warn | cap."""

    __tablename__ = "budget_config"
    __table_args__ = (
        CheckConstraint("mode IN ('observe', 'warn', 'cap')", name="ck_budget_config_mode"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    per_task_limit: Mapped[float | None] = mapped_column(Float)
    per_batch_limit: Mapped[float | None] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="observe", server_default=text("'observe'")
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
