"""Add phase 03-F audit, output validation, and budget infrastructure.

Revision ID: 0005_add_audit_and_budget
Revises: 0004_add_management_domain
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_add_audit_and_budget"
down_revision: Union[str, Sequence[str], None] = "0004_add_management_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite cannot alter a check constraint in place, and alembic batch-mode
# rebuilds move the priority check from an inline column constraint to a
# table-level constraint, which breaks SQLite DROP COLUMN during downgrades.
# We therefore rebuild media_jobs with explicit native DDL, keeping the
# ck_media_jobs_priority check inline exactly as phase 03-A defined it.

_STATUS_COLUMNS = """
    job_id VARCHAR(36) NOT NULL,
    workflow_type VARCHAR(128) NOT NULL,
    workflow_key VARCHAR(512) NOT NULL,
    executor_kind VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    node_id VARCHAR(128),
    status VARCHAR(32) DEFAULT 'queued' NOT NULL,
    input_json JSON NOT NULL,
    input_assets_json JSON NOT NULL,
    comfyui_prompt_id VARCHAR(128),
    submission_token VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(255),
    request_hash VARCHAR(64) NOT NULL,
    retry_count INTEGER DEFAULT 0 NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    deadline_at DATETIME,
    submit_started_at DATETIME,
    cancel_requested_at DATETIME,
    error_category VARCHAR(64),
    error_message TEXT,
    output_metadata JSON NOT NULL,
    lease_owner VARCHAR(255),
    lease_expires_at DATETIME,
    heartbeat_at DATETIME,
    next_attempt_at DATETIME,
    version INTEGER DEFAULT 1 NOT NULL,
    remote_status VARCHAR(32) DEFAULT 'unknown' NOT NULL,
    remote_termination_status VARCHAR(32) DEFAULT 'unknown' NOT NULL,
    remote_status_updated_at DATETIME,
    retry_of_job_id VARCHAR(36),
    priority INTEGER DEFAULT 1 NOT NULL CONSTRAINT ck_media_jobs_priority
        CHECK (priority IN (0, 1, 2)),
    estimated_cost FLOAT,
    actual_cost FLOAT,
    budget_warning TEXT
"""


def _set_foreign_keys(enabled: bool) -> None:
    """Toggle SQLite foreign keys outside any transaction (PRAGMA is a no-op inside one)."""
    value = "ON" if enabled else "OFF"
    bind = op.get_bind()
    try:
        bind.commit()
    except Exception:
        pass
    bind.execution_options(isolation_level="AUTOCOMMIT").execute(
        sa.text(f"PRAGMA foreign_keys={value}")
    )


def _create_media_jobs_table(*, status_values: str) -> None:
    _set_foreign_keys(False)
    try:
        op.execute(
            sa.text(
                "CREATE TABLE _media_jobs_0005 ("
                + _STATUS_COLUMNS
                + ",\n"
                "    PRIMARY KEY (job_id),\n"
                "    CONSTRAINT ck_media_jobs_retry_count CHECK (retry_count >= 0),\n"
                "    CONSTRAINT ck_media_jobs_remote_status CHECK (remote_status IN "
                "('unknown', 'queued', 'running', 'completed', 'failed')),\n"
                "    CONSTRAINT ck_media_jobs_remote_termination_status CHECK "
                "(remote_termination_status IN ('unknown', 'cancellation_requested', "
                "'cancelled', 'still_running', 'termination_unknown')),\n"
                "    CONSTRAINT ck_media_jobs_status CHECK (status IN (" + status_values + ")),\n"
                "    CONSTRAINT ck_media_jobs_version CHECK (version >= 1),\n"
                "    CONSTRAINT uq_media_jobs_idempotency_key UNIQUE (idempotency_key)\n"
                ")"
            )
        )
        op.execute(
            sa.text("INSERT INTO _media_jobs_0005 SELECT * FROM media_jobs")
        )
        op.execute(sa.text("DROP TABLE media_jobs"))
        op.execute(sa.text("ALTER TABLE _media_jobs_0005 RENAME TO media_jobs"))
    finally:
        _set_foreign_keys(True)
    op.execute(
        sa.text("CREATE INDEX ix_media_jobs_status ON media_jobs (status)")
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_media_jobs_status_next_attempt "
            "ON media_jobs (status, next_attempt_at)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_media_jobs_next_attempt_at "
            "ON media_jobs (next_attempt_at)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_media_jobs_lease_expires_at "
            "ON media_jobs (lease_expires_at)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_media_jobs_retry_of_job_id "
            "ON media_jobs (retry_of_job_id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_media_jobs_claim_priority ON media_jobs "
            "(status, priority DESC, next_attempt_at, created_at, job_id)"
        )
    )


def upgrade() -> None:
    # --- F1: audit events ---------------------------------------------------
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("scope_type", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column(
            "operator",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'system'"),
        ),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("cost_snapshot", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_audit_events_scope_created",
        "audit_events",
        ["scope_type", "scope_id", "created_at"],
        unique=False,
    )

    # --- F4: budget configuration (single global row) ----------------------
    op.create_table(
        "budget_config",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("per_task_limit", sa.Float(), nullable=True),
        sa.Column("per_batch_limit", sa.Float(), nullable=True),
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'observe'"),
        ),
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
        sa.CheckConstraint(
            "mode IN ('observe', 'warn', 'cap')",
            name="ck_budget_config_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "budget_config",
            sa.column("id", sa.String()),
            sa.column("per_task_limit", sa.Float()),
            sa.column("per_batch_limit", sa.Float()),
            sa.column("mode", sa.String()),
        ),
        [
            {
                "id": "global",
                "per_task_limit": None,
                "per_batch_limit": None,
                "mode": "observe",
            }
        ],
    )

    # --- F3: output contract schemas ---------------------------------------
    op.create_table(
        "output_schemas",
        sa.Column("schema_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_type", sa.String(length=128), nullable=False),
        sa.Column("schema_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("schema_id"),
        sa.UniqueConstraint(
            "workflow_type",
            name="uq_output_schemas_workflow_type",
        ),
    )
    # Two representative schemas: one T2V and one I2V.
    op.bulk_insert(
        sa.table(
            "output_schemas",
            sa.column("schema_id", sa.String()),
            sa.column("workflow_type", sa.String()),
            sa.column("schema_json", sa.JSON()),
        ),
        [
            {
                "schema_id": "schema-t2v-33f",
                "workflow_type": "a800_wan22_t2v_33f",
                "schema_json": {
                    "workflow_type": "a800_wan22_t2v_33f",
                    "expected_outputs": [
                        {
                            "media_type": "video",
                            "mime_type_pattern": "^video/",
                            "min_size_bytes": 1024,
                            "max_size_bytes": 1_073_741_824,
                            "min_duration": 0.5,
                            "max_duration": 30.0,
                            "min_width": 128,
                            "min_height": 128,
                        }
                    ],
                },
            },
            {
                "schema_id": "schema-i2v-33f",
                "workflow_type": "gpu_4090_wan21_i2v_33f",
                "schema_json": {
                    "workflow_type": "gpu_4090_wan21_i2v_33f",
                    "expected_outputs": [
                        {
                            "media_type": "video",
                            "mime_type_pattern": "^video/",
                            "min_size_bytes": 1024,
                            "max_size_bytes": 1_073_741_824,
                            "min_duration": 0.5,
                            "max_duration": 30.0,
                            "min_width": 128,
                            "min_height": 128,
                        }
                    ],
                },
            },
        ],
    )

    # --- F1: management operations cost snapshot ---------------------------
    op.add_column(
        "management_operations",
        sa.Column("cost_snapshot", sa.JSON(), nullable=True),
    )

    # --- F4: media job cost fields ------------------------------------------
    op.add_column(
        "media_jobs",
        sa.Column("estimated_cost", sa.Float(), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("actual_cost", sa.Float(), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("budget_warning", sa.Text(), nullable=True),
    )

    # --- F2: rebuild media_jobs so the status check allows awaiting_human ---
    _create_media_jobs_table(
        status_values="'queued', 'submitting', 'running', 'succeeded', 'failed', "
        "'cancelled', 'timed_out', 'awaiting_human'"
    )


def downgrade() -> None:
    # Restore the exact phase 03-A schema: seven statuses, inline priority check.
    _create_media_jobs_table(
        status_values="'queued', 'submitting', 'running', 'succeeded', 'failed', "
        "'cancelled', 'timed_out'"
    )
    op.drop_column("media_jobs", "budget_warning")
    op.drop_column("media_jobs", "actual_cost")
    op.drop_column("media_jobs", "estimated_cost")
    op.drop_column("management_operations", "cost_snapshot")
    op.drop_index("ix_audit_events_scope_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("budget_config")
    op.drop_table("output_schemas")
