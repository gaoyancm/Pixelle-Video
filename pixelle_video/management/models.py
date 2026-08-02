"""SQLAlchemy models for projects, batches, logical items, and operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_archived_updated_id", "archived_at", "updated_at", "id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
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
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProductionBatch(Base):
    __tablename__ = "production_batches"
    __table_args__ = (
        CheckConstraint("state IN ('draft', 'submitted')", name="ck_production_batches_state"),
        CheckConstraint(
            "default_priority IN (0, 1, 2)",
            name="ck_production_batches_default_priority",
        ),
        CheckConstraint("version >= 1", name="ck_production_batches_version"),
        CheckConstraint(
            "item_limit_snapshot IS NULL OR item_limit_snapshot > 0",
            name="ck_production_batches_item_limit_snapshot",
        ),
        Index(
            "ix_production_batches_project_created_id",
            "project_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_production_batches_project_archived_created",
            "project_id",
            "archived_at",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    common_parameters_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    default_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    item_limit_snapshot: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
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
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class ProductionItem(Base):
    __tablename__ = "production_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_production_items_batch_position"),
        CheckConstraint("position >= 0", name="ck_production_items_position"),
        CheckConstraint(
            "priority_override IS NULL OR priority_override IN (0, 1, 2)",
            name="ck_production_items_priority_override",
        ),
        Index("ix_production_items_batch_position", "batch_id", "position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("production_batches.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    parameter_overrides_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    effective_parameters_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    priority_override: Mapped[int | None] = mapped_column(Integer)
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


class ProductionItemAsset(Base):
    __tablename__ = "production_item_assets"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_production_item_assets_position"),
        UniqueConstraint(
            "item_id", "role", "position", name="uq_production_item_assets_item_role_position"
        ),
        UniqueConstraint(
            "item_id", "asset_id", "role", name="uq_production_item_assets_item_asset_role"
        ),
        Index("ix_production_item_assets_asset_id", "asset_id"),
    )

    item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("production_items.id", ondelete="RESTRICT"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ProductionItemAttempt(Base):
    __tablename__ = "production_item_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no > 0", name="ck_production_item_attempts_attempt_no"),
        CheckConstraint(
            "retry_of_attempt_no IS NULL OR retry_of_attempt_no > 0",
            name="ck_production_item_attempts_retry_of_attempt_no",
        ),
        UniqueConstraint("media_job_id", name="uq_production_item_attempts_media_job_id"),
        Index("ix_production_item_attempts_item_attempt", "item_id", "attempt_no"),
    )

    item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("production_items.id", ondelete="RESTRICT"), primary_key=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_jobs.job_id", ondelete="RESTRICT"), nullable=False
    )
    retry_of_attempt_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class ManagementOperation(Base):
    __tablename__ = "management_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('batch_submit', 'batch_cancel', 'retry_eligible')",
            name="ck_management_operations_operation_type",
        ),
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "operation_type",
            "idempotency_key",
            name="uq_management_operations_scope_operation_key",
        ),
        Index(
            "ix_management_operations_scope_lookup",
            "scope_type",
            "scope_id",
            "operation_type",
            "idempotency_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
