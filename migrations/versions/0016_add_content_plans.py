"""Add the phase 04-E LLM orchestration content-plan table.

Revision ID: 0016_add_content_plans
Revises: 0015_add_anime_series
Create Date: 2026-08-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_add_content_plans"
down_revision: Union[str, Sequence[str], None] = "0015_add_anime_series"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("cost_estimate", sa.Float(), nullable=True),
        sa.Column("checkpoint_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'generating', 'awaiting_approval', 'approved', 'rejected', 'completed', 'stage_failed')",
            name="ck_content_plans_status",
        ),
        sa.CheckConstraint(
            "intent IN ('product_ad', 'short_video', 'animation', 'unknown')",
            name="ck_content_plans_intent",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("content_plans")
