"""Create the persistent media_jobs table.

Revision ID: 0001_create_media_jobs
Revises:
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_create_media_jobs"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_type", sa.String(length=128), nullable=False),
        sa.Column("workflow_key", sa.String(length=512), nullable=False),
        sa.Column("executor_kind", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_assets_json", sa.JSON(), nullable=False),
        sa.Column("comfyui_prompt_id", sa.String(length=128), nullable=True),
        sa.Column("submission_token", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submit_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("output_metadata", sa.JSON(), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "remote_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column(
            "remote_termination_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column("remote_status_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("retry_count >= 0", name="ck_media_jobs_retry_count"),
        sa.CheckConstraint(
            "remote_status IN ('unknown', 'queued', 'running', 'completed', 'failed')",
            name="ck_media_jobs_remote_status",
        ),
        sa.CheckConstraint(
            "remote_termination_status IN ('unknown', 'cancellation_requested', "
            "'cancelled', 'still_running', 'termination_unknown')",
            name="ck_media_jobs_remote_termination_status",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'submitting', 'running', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_media_jobs_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_media_jobs_version"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_media_jobs_idempotency_key"),
    )
    op.create_index("ix_media_jobs_status", "media_jobs", ["status"], unique=False)
    op.create_index(
        "ix_media_jobs_status_next_attempt",
        "media_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_jobs_next_attempt_at",
        "media_jobs",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_jobs_lease_expires_at",
        "media_jobs",
        ["lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_jobs_lease_expires_at", table_name="media_jobs")
    op.drop_index("ix_media_jobs_next_attempt_at", table_name="media_jobs")
    op.drop_index("ix_media_jobs_status_next_attempt", table_name="media_jobs")
    op.drop_index("ix_media_jobs_status", table_name="media_jobs")
    op.drop_table("media_jobs")
