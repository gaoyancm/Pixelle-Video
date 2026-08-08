"""SQLAlchemy persistence model for phase 03-F audit events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index(
            "ix_audit_events_scope_created",
            "scope_type",
            "scope_id",
            "created_at",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operator: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", server_default=text("'system'")
    )
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
