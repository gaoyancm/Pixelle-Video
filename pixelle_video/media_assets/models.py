"""SQLAlchemy models for managed media assets and job relations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint("kind IN ('input', 'output')", name="ck_media_assets_kind"),
        CheckConstraint(
            "state IN ('available', 'disabled', 'deleted', 'missing')",
            name="ck_media_assets_state",
        ),
        CheckConstraint("backend = 'local'", name="ck_media_assets_backend"),
        CheckConstraint("source IN ('upload', 'generated', 'trusted_import')", name="ck_media_assets_source"),
        CheckConstraint("size_bytes >= 0", name="ck_media_assets_size_bytes"),
        UniqueConstraint("object_key", name="uq_media_assets_object_key"),
        UniqueConstraint("idempotency_key", name="uq_media_assets_idempotency_key"),
        Index("ix_media_assets_kind_state_created", "kind", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="available", server_default=text("'available'")
    )
    backend: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local", server_default=text("'local'")
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    request_hash: Mapped[str | None] = mapped_column(String(64))
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
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MediaJobAsset(Base):
    __tablename__ = "media_job_assets"
    __table_args__ = (
        CheckConstraint("direction IN ('input', 'output')", name="ck_media_job_assets_direction"),
        CheckConstraint("position >= 0", name="ck_media_job_assets_position"),
        UniqueConstraint(
            "job_id", "direction", "position", name="uq_media_job_assets_job_direction_position"
        ),
        UniqueConstraint(
            "job_id", "asset_id", "direction", name="uq_media_job_assets_job_asset_direction"
        ),
        Index("ix_media_job_assets_job_direction", "job_id", "direction"),
        Index("ix_media_job_assets_asset_id", "asset_id"),
    )

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_jobs.job_id", ondelete="RESTRICT"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="RESTRICT"), primary_key=True
    )
    direction: Mapped[str] = mapped_column(String(16), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
