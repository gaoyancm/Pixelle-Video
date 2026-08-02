"""Add the phase 03-A management domain and media-job priority.

Revision ID: 0004_add_management_domain
Revises: 0003_add_media_assets
Create Date: 2026-08-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_management_domain"
down_revision: Union[str, Sequence[str], None] = "0003_add_media_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_projects_archived_updated_id",
        "projects",
        ["archived_at", "updated_at", "id"],
        unique=False,
    )

    op.create_table(
        "production_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("workflow_type", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "common_parameters_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("default_priority", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("item_limit_snapshot", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(
            "default_priority IN (0, 1, 2)",
            name="ck_production_batches_default_priority",
        ),
        sa.CheckConstraint(
            "item_limit_snapshot IS NULL OR item_limit_snapshot > 0",
            name="ck_production_batches_item_limit_snapshot",
        ),
        sa.CheckConstraint("state IN ('draft', 'submitted')", name="ck_production_batches_state"),
        sa.CheckConstraint("version >= 1", name="ck_production_batches_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_production_batches_project_archived_created",
        "production_batches",
        ["project_id", "archived_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_production_batches_project_created_id",
        "production_batches",
        ["project_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "production_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "parameter_overrides_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("effective_parameters_json", sa.JSON(), nullable=True),
        sa.Column("priority_override", sa.Integer(), nullable=True),
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
        sa.CheckConstraint("position >= 0", name="ck_production_items_position"),
        sa.CheckConstraint(
            "priority_override IS NULL OR priority_override IN (0, 1, 2)",
            name="ck_production_items_priority_override",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["production_batches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "position", name="uq_production_items_batch_position"),
    )
    op.create_index(
        "ix_production_items_batch_position",
        "production_items",
        ["batch_id", "position"],
        unique=False,
    )

    op.create_table(
        "management_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("operation_type", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "operation_type IN ('batch_submit', 'batch_cancel', 'retry_eligible')",
            name="ck_management_operations_operation_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "operation_type",
            "idempotency_key",
            name="uq_management_operations_scope_operation_key",
        ),
    )
    op.create_index(
        "ix_management_operations_scope_lookup",
        "management_operations",
        ["scope_type", "scope_id", "operation_type", "idempotency_key"],
        unique=False,
    )

    op.create_table(
        "production_item_assets",
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_production_item_assets_position"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["item_id"], ["production_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("item_id", "asset_id", "role"),
        sa.UniqueConstraint(
            "item_id",
            "asset_id",
            "role",
            name="uq_production_item_assets_item_asset_role",
        ),
        sa.UniqueConstraint(
            "item_id",
            "role",
            "position",
            name="uq_production_item_assets_item_role_position",
        ),
    )
    op.create_index(
        "ix_production_item_assets_asset_id",
        "production_item_assets",
        ["asset_id"],
        unique=False,
    )

    op.create_table(
        "production_item_attempts",
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("media_job_id", sa.String(length=36), nullable=False),
        sa.Column("retry_of_attempt_no", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_production_item_attempts_attempt_no"),
        sa.CheckConstraint(
            "retry_of_attempt_no IS NULL OR retry_of_attempt_no > 0",
            name="ck_production_item_attempts_retry_of_attempt_no",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["production_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["media_job_id"], ["media_jobs.job_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("item_id", "attempt_no"),
        sa.UniqueConstraint("media_job_id", name="uq_production_item_attempts_media_job_id"),
    )
    op.create_index(
        "ix_production_item_attempts_item_attempt",
        "production_item_attempts",
        ["item_id", "attempt_no"],
        unique=False,
    )

    op.add_column(
        "media_jobs",
        sa.Column(
            "priority",
            sa.Integer(),
            sa.CheckConstraint("priority IN (0, 1, 2)", name="ck_media_jobs_priority"),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_index(
        "ix_media_jobs_claim_priority",
        "media_jobs",
        ["status", sa.text("priority DESC"), "next_attempt_at", "created_at", "job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_production_item_attempts_item_attempt", table_name="production_item_attempts")
    op.drop_table("production_item_attempts")
    op.drop_index("ix_production_item_assets_asset_id", table_name="production_item_assets")
    op.drop_table("production_item_assets")
    op.drop_index("ix_management_operations_scope_lookup", table_name="management_operations")
    op.drop_table("management_operations")
    op.drop_index("ix_production_items_batch_position", table_name="production_items")
    op.drop_table("production_items")
    op.drop_index("ix_production_batches_project_created_id", table_name="production_batches")
    op.drop_index(
        "ix_production_batches_project_archived_created",
        table_name="production_batches",
    )
    op.drop_table("production_batches")
    op.drop_index("ix_projects_archived_updated_id", table_name="projects")
    op.drop_table("projects")
    op.drop_index("ix_media_jobs_claim_priority", table_name="media_jobs")
    op.drop_column("media_jobs", "priority")
