"""SQLAlchemy persistence model for media jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, TypeDecorator

from .state_machine import JobStatus, RemoteJobStatus, RemoteTerminationStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Normalize bound and loaded timestamps to timezone-aware UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


def _enum_values(enum_type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class MediaJob(Base):
    """Current durable state of one platform media-generation job."""

    __tablename__ = "media_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_media_jobs_idempotency_key"),
        CheckConstraint(
            f"status IN ({_enum_values(JobStatus)})",
            name="ck_media_jobs_status",
        ),
        CheckConstraint(
            f"remote_status IN ({_enum_values(RemoteJobStatus)})",
            name="ck_media_jobs_remote_status",
        ),
        CheckConstraint(
            f"remote_termination_status IN ({_enum_values(RemoteTerminationStatus)})",
            name="ck_media_jobs_remote_termination_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_media_jobs_retry_count"),
        CheckConstraint("priority IN (0, 1, 2)", name="ck_media_jobs_priority"),
        CheckConstraint("version >= 1", name="ck_media_jobs_version"),
        Index("ix_media_jobs_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_media_jobs_lease_expires_at", "lease_expires_at"),
        Index(
            "ix_media_jobs_claim_priority",
            "status",
            desc("priority"),
            "next_attempt_at",
            "created_at",
            "job_id",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_type: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_key: Mapped[str] = mapped_column(String(512), nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobStatus.QUEUED.value,
        server_default=text("'queued'"),
        index=True,
    )
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_assets_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    comfyui_prompt_id: Mapped[str | None] = mapped_column(String(128))
    submission_token: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    retry_of_job_id: Mapped[str | None] = mapped_column(String(36), index=True)

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
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    submit_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    error_category: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    output_metadata: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )

    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    remote_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RemoteJobStatus.UNKNOWN.value,
        server_default=text("'unknown'"),
    )
    remote_termination_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RemoteTerminationStatus.UNKNOWN.value,
        server_default=text("'unknown'"),
    )
    remote_status_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
