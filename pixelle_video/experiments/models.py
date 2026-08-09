"""SQLAlchemy persistence models for the phase 04-C experiment system."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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

EXPERIMENT_METRICS = ("cost", "quality", "composite")
EXPERIMENT_STATUSES = ("active", "completed", "archived")


class Experiment(Base):
    """One experiment comparing prompt/model/parameter variants."""

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint("metric IN ('cost', 'quality', 'composite')", name="ck_experiments_metric"),
        CheckConstraint(
            "status IN ('active', 'completed', 'archived')", name="ck_experiments_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(
        String(32), nullable=False, default="composite", server_default=text("'composite'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class ExperimentGroup(Base):
    """One variant arm inside an experiment."""

    __tablename__ = "experiment_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    group_name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str | None] = mapped_column(String(36))
    model_name: Mapped[str | None] = mapped_column(String(64))
    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class ExperimentJob(Base):
    """Links one media job to an experiment group."""

    __tablename__ = "experiment_jobs"

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_jobs.job_id", ondelete="RESTRICT"), primary_key=True
    )
    experiment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("experiment_groups.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class FailureSample(Base):
    """A failed experiment case fed back for prompt improvement."""

    __tablename__ = "failure_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_jobs.job_id", ondelete="RESTRICT"), nullable=False
    )
    experiment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("experiments.id", ondelete="SET NULL")
    )
    prompt_version_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str | None] = mapped_column(Text)
    qc_issues_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
