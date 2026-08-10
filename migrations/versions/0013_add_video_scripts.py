"""Add the phase 06 short-video script table.

Revision ID: 0013_add_video_scripts
Revises: 0012_seed_ad_prompt
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_add_video_scripts"
down_revision: Union[str, Sequence[str], None] = "0012_seed_ad_prompt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_scripts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("topic", sa.String(length=512), nullable=False),
        sa.Column(
            "language",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'zh-CN'"),
        ),
        sa.Column(
            "target_duration",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'tiktok'"),
        ),
        sa.Column("script_json", sa.JSON(), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
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
            "status IN ('draft', 'confirmed', 'storyboarding', 'assets', 'composing', 'completed', 'archived')",
            name="ck_video_scripts_status",
        ),
        sa.CheckConstraint(
            "target_duration > 0",
            name="ck_video_scripts_target_duration",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("video_scripts")
