"""Add the phase 04-B automatic QC pipeline tables and seeds.

Revision ID: 0007_add_qc_rules
Revises: 0006_add_prompt_templates
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_add_qc_rules"
down_revision: Union[str, Sequence[str], None] = "0006_add_prompt_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

QC_CATEGORIES = ("technical", "visual", "character", "narrative", "brand", "platform")
QC_RULE_TYPES = ("schema_validation", "parameter_check", "text_analysis")


def upgrade() -> None:
    op.create_table(
        "qc_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("rule_type", sa.String(length=32), nullable=False),
        sa.Column("rule_config_json", sa.JSON(), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'local'"),
        ),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("1")),
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
            "category IN ('technical', 'visual', 'character', 'narrative', 'brand', 'platform')",
            name="ck_qc_rules_category",
        ),
        sa.CheckConstraint(
            "rule_type IN ('schema_validation', 'parameter_check', 'text_analysis')",
            name="ck_qc_rules_rule_type",
        ),
        sa.CheckConstraint("is_active IN (0, 1)", name="ck_qc_rules_is_active"),
        sa.CheckConstraint("priority >= 1", name="ck_qc_rules_priority"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_qc_rules_name"),
    )

    op.create_table(
        "qc_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rules_json", sa.JSON(), nullable=False),
        sa.Column("is_default", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("is_default IN (0, 1)", name="ck_qc_profiles_is_default"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_qc_profiles_name"),
    )

    # --- Q1 seed: 12 rules covering all six QC categories -------------------
    qc_rules = sa.table(
        "qc_rules",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("rule_type", sa.String()),
        sa.column("rule_config_json", sa.JSON()),
        sa.column("provider", sa.String()),
        sa.column("is_active", sa.Integer()),
        sa.column("priority", sa.Integer()),
    )
    op.bulk_insert(
        qc_rules,
        [
            {
                "id": "qc-resolution",
                "name": "分辨率检查",
                "category": "technical",
                "rule_type": "schema_validation",
                "rule_config_json": {
                    "field": "resolution",
                    "operator": "min",
                    "expected": "1280x720",
                    "severity": "major",
                },
                "provider": "ffprobe",
                "is_active": 1,
                "priority": 1,
            },
            {
                "id": "qc-frame-rate",
                "name": "帧率检查",
                "category": "technical",
                "rule_type": "schema_validation",
                "rule_config_json": {
                    "field": "frame_rate",
                    "operator": "range",
                    "expected": "20-30",
                    "severity": "minor",
                },
                "provider": "ffprobe",
                "is_active": 1,
                "priority": 2,
            },
            {
                "id": "qc-file-size",
                "name": "文件大小检查",
                "category": "technical",
                "rule_type": "parameter_check",
                "rule_config_json": {
                    "field": "size_bytes",
                    "operator": "range",
                    "expected": "1024-1073741824",
                    "severity": "minor",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 3,
            },
            {
                "id": "qc-audio-channel",
                "name": "音频声道检查",
                "category": "technical",
                "rule_type": "schema_validation",
                "rule_config_json": {
                    "field": "audio_channels",
                    "operator": "in",
                    "expected": "1,2",
                    "severity": "major",
                },
                "provider": "ffprobe",
                "is_active": 1,
                "priority": 4,
            },
            {
                "id": "qc-frame-stability",
                "name": "画面稳定性",
                "category": "visual",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "frame_diff",
                    "operator": "max",
                    "expected": "0.8",
                    "severity": "major",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 5,
            },
            {
                "id": "qc-brightness",
                "name": "亮度对比度检查",
                "category": "visual",
                "rule_type": "parameter_check",
                "rule_config_json": {
                    "field": "brightness",
                    "operator": "range",
                    "expected": "0.2-0.8",
                    "severity": "minor",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 6,
            },
            {
                "id": "qc-character-consistency",
                "name": "角色一致性",
                "category": "character",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "character_ref",
                    "operator": "eq",
                    "expected": "consistent",
                    "severity": "critical",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 7,
            },
            {
                "id": "qc-subtitle-completeness",
                "name": "字幕完整性",
                "category": "narrative",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "subtitle_complete",
                    "operator": "eq",
                    "expected": "true",
                    "severity": "major",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 8,
            },
            {
                "id": "qc-brand-color",
                "name": "品牌色检查",
                "category": "brand",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "brand_color",
                    "operator": "eq",
                    "expected": "true",
                    "severity": "major",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 9,
            },
            {
                "id": "qc-product-info",
                "name": "产品信息准确性",
                "category": "brand",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "product_info",
                    "operator": "eq",
                    "expected": "accurate",
                    "severity": "critical",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 10,
            },
            {
                "id": "qc-nsfw",
                "name": "违规内容检测",
                "category": "platform",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "nsfw",
                    "operator": "eq",
                    "expected": "false",
                    "severity": "critical",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 11,
            },
            {
                "id": "qc-copyright-risk",
                "name": "版权风险",
                "category": "platform",
                "rule_type": "text_analysis",
                "rule_config_json": {
                    "field": "copyright_risk",
                    "operator": "eq",
                    "expected": "false",
                    "severity": "major",
                },
                "provider": "local",
                "is_active": 1,
                "priority": 12,
            },
        ],
    )

    # --- Q1 seed: two profiles (default / strict) ----------------------------
    qc_profiles = sa.table(
        "qc_profiles",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("rules_json", sa.JSON()),
        sa.column("is_default", sa.Integer()),
    )
    all_rule_ids = [
        "qc-resolution",
        "qc-frame-rate",
        "qc-file-size",
        "qc-audio-channel",
        "qc-frame-stability",
        "qc-brightness",
        "qc-character-consistency",
        "qc-subtitle-completeness",
        "qc-brand-color",
        "qc-product-info",
        "qc-nsfw",
        "qc-copyright-risk",
    ]
    op.bulk_insert(
        qc_profiles,
        [
            {
                "id": "profile-default",
                "name": "短视频默认QC",
                "description": "覆盖 6 类 QC 的默认检查方案",
                "rules_json": [
                    {"rule_id": rule_id, "severity_override": None} for rule_id in all_rule_ids
                ],
                "is_default": 1,
            },
            {
                "id": "profile-strict",
                "name": "广告严格QC",
                "description": "广告素材严格检查：分辨率与产品信息升级为 critical",
                "rules_json": [
                    {"rule_id": "qc-resolution", "severity_override": "critical"},
                    {"rule_id": "qc-product-info", "severity_override": "critical"},
                    *[
                        {"rule_id": rule_id, "severity_override": None}
                        for rule_id in all_rule_ids
                        if rule_id not in ("qc-resolution", "qc-product-info")
                    ],
                ],
                "is_default": 0,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("qc_profiles")
    op.drop_table("qc_rules")
