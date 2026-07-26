"""Add managed media assets and durable job-asset relations.

Revision ID: 0003_add_media_assets
Revises: 0002_add_media_job_retry_lineage
Create Date: 2026-07-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_media_assets"
down_revision: Union[str, Sequence[str], None] = "0002_add_media_job_retry_lineage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="'available'"),
        sa.Column("backend", sa.String(length=16), nullable=False, server_default="'local'"),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
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
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("backend = 'local'", name="ck_media_assets_backend"),
        sa.CheckConstraint("kind IN ('input', 'output')", name="ck_media_assets_kind"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_media_assets_size_bytes"),
        sa.CheckConstraint(
            "source IN ('upload', 'generated', 'trusted_import')",
            name="ck_media_assets_source",
        ),
        sa.CheckConstraint(
            "state IN ('available', 'disabled', 'deleted', 'missing')",
            name="ck_media_assets_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_media_assets_idempotency_key"),
        sa.UniqueConstraint("object_key", name="uq_media_assets_object_key"),
    )
    op.create_index(
        "ix_media_assets_kind_state_created",
        "media_assets",
        ["kind", "state", "created_at"],
        unique=False,
    )
    op.create_table(
        "media_job_assets",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "direction IN ('input', 'output')", name="ck_media_job_assets_direction"
        ),
        sa.CheckConstraint("position >= 0", name="ck_media_job_assets_position"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["media_assets.id"], name="fk_media_job_assets_asset_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["media_jobs.job_id"], name="fk_media_job_assets_job_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("job_id", "asset_id", "direction"),
        sa.UniqueConstraint(
            "job_id",
            "asset_id",
            "direction",
            name="uq_media_job_assets_job_asset_direction",
        ),
        sa.UniqueConstraint(
            "job_id",
            "direction",
            "position",
            name="uq_media_job_assets_job_direction_position",
        ),
    )
    op.create_index(
        "ix_media_job_assets_asset_id", "media_job_assets", ["asset_id"], unique=False
    )
    op.create_index(
        "ix_media_job_assets_job_direction",
        "media_job_assets",
        ["job_id", "direction"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_job_assets_job_direction", table_name="media_job_assets")
    op.drop_index("ix_media_job_assets_asset_id", table_name="media_job_assets")
    op.drop_table("media_job_assets")
    op.drop_index("ix_media_assets_kind_state_created", table_name="media_assets")
    op.drop_table("media_assets")
