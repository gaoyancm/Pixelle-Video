"""Add the phase 04-D knowledge base tables.

Revision ID: 0009_add_knowledge
Revises: 0008_add_experiments
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_add_knowledge"
down_revision: Union[str, Sequence[str], None] = "0008_add_experiments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KNOWLEDGE_CATEGORIES = (
    "策划",
    "平台规则",
    "镜头叙事",
    "角色场景",
    "模型工作流",
    "品牌产品",
    "后处理",
    "故障诊断",
)
EVIDENCE_CLASSES = ("documented_fact", "empirical_observation", "production_heuristic")
ENTRY_STATUSES = ("draft", "published", "archived")


def upgrade() -> None:
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column(
            "evidence_class",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'production_heuristic'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("source_doc", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
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
            "category IN ('策划', '平台规则', '镜头叙事', '角色场景', '模型工作流', '品牌产品', '后处理', '故障诊断')",
            name="ck_knowledge_entries_category",
        ),
        sa.CheckConstraint(
            "evidence_class IN ('documented_fact', 'empirical_observation', 'production_heuristic')",
            name="ck_knowledge_entries_evidence_class",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_knowledge_entries_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "knowledge_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_knowledge_tags_name"),
    )

    op.create_table(
        "knowledge_entry_tags",
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["knowledge_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["knowledge_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entry_id", "tag_id"),
    )

    op.create_table(
        "knowledge_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("link_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "target_type IN ('prompt_template', 'qc_rule', 'workflow')",
            name="ck_knowledge_links_target_type",
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["knowledge_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_links")
    op.drop_table("knowledge_entry_tags")
    op.drop_table("knowledge_tags")
    op.drop_table("knowledge_entries")
