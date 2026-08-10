"""Seed the phase 05 ad-hook prompt template (double-brace variables).

Revision ID: 0012_seed_ad_prompt
Revises: 0011_add_product_briefs
Create Date: 2026-08-09

Data-only migration registering one ad-copy prompt template in the
phase 04-A prompt_templates table with {{product}}/{{points}}/{{audience}}
variables, so the phase 05 generate-ideas endpoint can compile it via the
04-A PromptCompiler. Idempotent by template id.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_seed_ad_prompt"
down_revision: Union[str, Sequence[str], None] = "0011_add_product_briefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TEMPLATE_ID = "pt-ad-hook"
TEMPLATE_NAME = "产品广告Hook模板"
TEMPLATE_TEXT = (
    "为产品「{{product}}」编写一条短视频/广告开场 Hook。\n\n"
    "核心卖点：{{points}}\n"
    "目标受众：{{audience}}\n\n"
    "要求：\n"
    "1. 前 1-2 秒抓住注意力（问题开场、反转、强悬念或直接利益点）\n"
    "2. 语言简洁有力，不超过 30 个字\n"
    "3. 突出 1-2 个核心卖点\n"
    "4. 结尾自然引出 CTA\n\n"
    "Hook："
)
VARIABLES = [
    {
        "name": "product",
        "type": "string",
        "required": True,
        "default": None,
        "description": "产品名称",
    },
    {
        "name": "points",
        "type": "string",
        "required": True,
        "default": None,
        "description": "核心卖点（顿号分隔）",
    },
    {
        "name": "audience",
        "type": "string",
        "required": True,
        "default": None,
        "description": "目标受众描述",
    },
]


def upgrade() -> None:
    connection = op.get_bind()
    prompt_templates = sa.table(
        "prompt_templates",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("template_text", sa.Text()),
        sa.column("variables_json", sa.JSON()),
        sa.column("provider", sa.String()),
        sa.column("is_active", sa.Integer()),
    )
    prompt_versions = sa.table(
        "prompt_versions",
        sa.column("id", sa.String()),
        sa.column("template_id", sa.String()),
        sa.column("version_no", sa.Integer()),
        sa.column("template_text", sa.Text()),
        sa.column("variables_json", sa.JSON()),
        sa.column("change_note", sa.Text()),
    )
    exists = connection.execute(
        sa.select(prompt_templates.c.id).where(prompt_templates.c.id == TEMPLATE_ID)
    ).scalar_one_or_none()
    if exists:
        return
    op.bulk_insert(
        prompt_templates,
        [
            {
                "id": TEMPLATE_ID,
                "name": TEMPLATE_NAME,
                "category": "广告",
                "description": "阶段05 产品广告 Hook 模板（generate-ideas 使用）",
                "template_text": TEMPLATE_TEXT,
                "variables_json": VARIABLES,
                "provider": "default",
                "is_active": 1,
            }
        ],
    )
    op.bulk_insert(
        prompt_versions,
        [
            {
                "id": f"pv-{TEMPLATE_ID}",
                "template_id": TEMPLATE_ID,
                "version_no": 1,
                "template_text": TEMPLATE_TEXT,
                "variables_json": VARIABLES,
                "change_note": "阶段05 登记广告 Hook 模板",
            }
        ],
    )


def downgrade() -> None:
    connection = op.get_bind()
    prompt_versions = sa.table("prompt_versions", sa.column("template_id", sa.String()))
    connection.execute(prompt_versions.delete().where(prompt_versions.c.template_id == TEMPLATE_ID))
    prompt_templates = sa.table("prompt_templates", sa.column("id", sa.String()))
    connection.execute(prompt_templates.delete().where(prompt_templates.c.id == TEMPLATE_ID))
