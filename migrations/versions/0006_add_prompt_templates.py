"""Add the phase 04-A prompt template versioning system.

Revision ID: 0006_add_prompt_templates
Revises: 0005_add_audit_and_budget
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_prompt_templates"
down_revision: Union[str, Sequence[str], None] = "0005_add_audit_and_budget"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column(
            "variables_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("current_score", sa.Integer(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("is_active IN (0, 1)", name="ck_prompt_templates_is_active"),
        sa.CheckConstraint(
            "current_score IS NULL OR current_score BETWEEN 1 AND 5",
            name="ck_prompt_templates_current_score",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_prompt_templates_name"),
    )

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("variables_json", sa.JSON(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_prompt_versions_version_no"),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id",
            "version_no",
            name="uq_prompt_versions_template_version",
        ),
    )

    op.create_table(
        "prompt_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_prompt_tags_name"),
    )

    op.create_table(
        "prompt_template_tags",
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["prompt_tags.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("template_id", "tag_id"),
    )

    # --- P1 seed: seven initial templates imported from pixelle_video/prompts/ ---
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
    op.bulk_insert(
        prompt_templates,
        [
            {
                "id": "pt-title-generation",
                "name": "视频标题生成",
                "category": "标题",
                "description": "根据内容生成简短吸引人的视频标题（来自 title_generation.py）",
                "template_text": (
                    "请为以下内容生成一个简短、有吸引力的标题。\n\n"
                    "内容：\n{{content}}\n\n"
                    "要求：\n"
                    "1. 标题必须与输入内容使用相同语言\n"
                    "2. 标题长度不得超过 {{max_length}} 个字符\n"
                    "3. 必须抓住内容的核心要点\n"
                    "4. 结尾不能带标点符号\n"
                    "5. 输出完整有意义的短语，不中途截断\n\n"
                    "标题："
                ),
                "variables_json": [
                    {
                        "name": "content",
                        "type": "string",
                        "required": True,
                        "default": None,
                        "description": "用于生成标题的内容（取前500字符）",
                    },
                    {
                        "name": "max_length",
                        "type": "integer",
                        "required": False,
                        "default": "15",
                        "description": "标题最大字符数",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
            {
                "id": "pt-content-narration",
                "name": "内容旁白生成",
                "category": "旁白",
                "description": "从用户内容中提取 N 段分镜旁白（来自 content_narration.py）",
                "template_text": (
                    "# 角色定义\n"
                    "你是专业的视频旁白创作师。\n\n"
                    "# 核心任务\n"
                    "用户将提供内容（可能长或短），你需要为 {{n_storyboard}} 个视频分镜提取旁白"
                    "（用于 TTS 生成视频音频）。\n\n"
                    "# 输入内容\n"
                    "{{content}}\n\n"
                    "# 输出要求\n"
                    "1. 每段旁白字数严格控制为 {{min_words}}~{{max_words}} 词\n"
                    "2. 共输出 {{n_storyboard}} 段旁白，对应 {{n_storyboard}} 个分镜\n"
                    '3. 严格按 JSON 格式输出 {"narrations": [旁白数组]}\n'
                    "4. 只输出 JSON，不加任何解释\n\n"
                    "现在，请从上述内容中提取 {{n_storyboard}} 段分镜旁白。"
                ),
                "variables_json": [
                    {
                        "name": "content",
                        "type": "string",
                        "required": True,
                        "default": None,
                        "description": "用户输入内容",
                    },
                    {
                        "name": "n_storyboard",
                        "type": "integer",
                        "required": True,
                        "default": "3",
                        "description": "分镜数量",
                    },
                    {
                        "name": "min_words",
                        "type": "integer",
                        "required": True,
                        "default": "50",
                        "description": "每段最少字数",
                    },
                    {
                        "name": "max_words",
                        "type": "integer",
                        "required": True,
                        "default": "100",
                        "description": "每段最多字数",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
            {
                "id": "pt-topic-narration",
                "name": "主题旁白生成",
                "category": "旁白",
                "description": "围绕主题创建 N 个分镜旁白，像与朋友聊天一样自然（来自 topic_narration.py）",
                "template_text": (
                    "# 角色定义\n"
                    "你是专业的视频旁白创作师。\n\n"
                    "# 核心任务\n"
                    "用户将输入一个主题。你需要为这个主题创建 {{n_storyboard}} 个视频分镜，"
                    "每个分镜包含「旁白」（用于 TTS 生成视频解释音频），自然且有价值，"
                    "像与朋友聊天一样，引起观众共鸣。\n\n"
                    "# 输入主题\n"
                    "{{topic}}\n\n"
                    "# 输出要求\n"
                    "1. 每段旁白字数严格控制为 {{min_words}}~{{max_words}} 词\n"
                    "2. 共输出 {{n_storyboard}} 段旁白\n"
                    '3. 严格按 JSON 格式输出 {"narrations": [旁白数组]}\n'
                    "4. 只输出 JSON，不加任何解释\n\n"
                    "现在，请为主题创建 {{n_storyboard}} 个分镜的旁白。"
                ),
                "variables_json": [
                    {
                        "name": "topic",
                        "type": "string",
                        "required": True,
                        "default": None,
                        "description": "视频主题",
                    },
                    {
                        "name": "n_storyboard",
                        "type": "integer",
                        "required": True,
                        "default": "3",
                        "description": "分镜数量",
                    },
                    {
                        "name": "min_words",
                        "type": "integer",
                        "required": True,
                        "default": "50",
                        "description": "每段最少字数",
                    },
                    {
                        "name": "max_words",
                        "type": "integer",
                        "required": True,
                        "default": "100",
                        "description": "每段最多字数",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
            {
                "id": "pt-video-generation",
                "name": "视频提示词生成",
                "category": "视频",
                "description": "根据分镜旁白生成英文视频生成提示词（来自 video_generation.py）",
                "template_text": (
                    "# 角色定义\n"
                    "你是专业的视频创意设计师，擅长为视频脚本创作动态且富有表现力的视频生成提示词。\n\n"
                    "# 核心任务\n"
                    "基于现有视频脚本，为每个分镜的「旁白内容」创建对应的**英文**视频生成提示词。\n\n"
                    "**重要：输入包含 {{narrations_count}} 条旁白，你必须为每条旁白生成一个视频提示词，"
                    "共 {{narrations_count}} 个视频提示词。**\n\n"
                    "# 输入内容\n"
                    "{{narrations_json}}\n\n"
                    "# 输出要求\n"
                    "1. 语言：必须使用英文\n"
                    "2. 描述结构：场景 + 角色动作 + 镜头运动 + 情绪 + 氛围\n"
                    "3. 描述长度：清晰、完整、有创意（建议 50-100 英文词）\n"
                    "4. 动态元素：强调动作、运动、变化等动态效果\n\n"
                    "# 输出格式\n"
                    "严格按以下 JSON 格式输出，视频提示词必须为英文：\n"
                    "```json\n"
                    '{"video_prompts": ["英文视频提示词"]}\n'
                    "```\n\n"
                    "现在，请为上述 {{narrations_count}} 条旁白创建对应的**英文**视频提示词。"
                    "只输出 JSON，不加其他内容。"
                ),
                "variables_json": [
                    {
                        "name": "narrations_json",
                        "type": "json",
                        "required": True,
                        "default": None,
                        "description": "旁白数组 JSON",
                    },
                    {
                        "name": "narrations_count",
                        "type": "integer",
                        "required": True,
                        "default": None,
                        "description": "旁白数量",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
            {
                "id": "pt-image-generation",
                "name": "图片提示词生成",
                "category": "图片",
                "description": "根据分镜旁白生成英文图片提示词（来自 image_generation.py）",
                "template_text": (
                    "# 角色定义\n"
                    "你是专业的图片创意设计师，擅长将叙事内容转化为生动的图片生成提示词。\n\n"
                    "# 核心任务\n"
                    "基于现有视频脚本，为每个分镜的「旁白内容」创建对应的**英文**图片提示词。\n\n"
                    "**重要：输入包含 {{narrations_count}} 条旁白，你必须为每条旁白生成一个图片提示词，"
                    "共 {{narrations_count}} 个图片提示词。**\n\n"
                    "# 输入内容\n"
                    "{{narrations_json}}\n\n"
                    "# 输出要求\n"
                    "1. 语言：必须使用英文\n"
                    "2. 描述结构：主体 + 场景 + 构图 + 光线 + 风格\n"
                    "3. 描述长度：清晰、完整、有创意\n"
                    "4. 强调画面表现力与叙事一致性\n\n"
                    "# 输出格式\n"
                    "严格按以下 JSON 格式输出，图片提示词必须为英文：\n"
                    "```json\n"
                    '{"image_prompts": ["英文图片提示词"]}\n'
                    "```\n\n"
                    "现在，请为上述 {{narrations_count}} 条旁白创建对应的**英文**图片提示词。"
                    "只输出 JSON，不加其他内容。"
                ),
                "variables_json": [
                    {
                        "name": "narrations_json",
                        "type": "json",
                        "required": True,
                        "default": None,
                        "description": "旁白数组 JSON",
                    },
                    {
                        "name": "narrations_count",
                        "type": "integer",
                        "required": True,
                        "default": None,
                        "description": "旁白数量",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
            {
                "id": "pt-style-conversion",
                "name": "风格转换提示词",
                "category": "图片",
                "description": "将风格描述转换为 Stable Diffusion/FLUX 的详细图片生成提示词（来自 style_conversion.py）",
                "template_text": (
                    "请将以下风格描述转换为 Stable Diffusion/FLUX 的详细图片生成提示词：\n\n"
                    "风格描述：{{description}}\n\n"
                    "要求：\n"
                    "1. 保留风格的核心特征\n"
                    "2. 补充光线、构图、材质等细节\n"
                    "3. 输出英文提示词\n\n"
                    "提示词："
                ),
                "variables_json": [
                    {
                        "name": "description",
                        "type": "string",
                        "required": True,
                        "default": None,
                        "description": "风格描述",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
            {
                "id": "pt-asset-script-generation",
                "name": "素材脚本生成",
                "category": "分镜",
                "description": "基于视频意图和可用素材生成视频脚本（来自 asset_script_generation.py）",
                "template_text": (
                    "你是专业的视频脚本创作者。基于用户的视频意图和可用素材，生成一个 "
                    "{{duration}} 秒的视频脚本。在开始之前，你需要检测用户的输入语言——"
                    "如果是英文，则所有文案必须使用英文。严格遵守用户输入语言作为标准。\n\n"
                    "# 输入\n"
                    "- 视频意图：{{intent}}\n"
                    "- 目标时长：{{duration}} 秒\n"
                    "- 可用素材：\n{{assets_text}}\n\n"
                    "# 输出要求\n"
                    "1. 脚本包含场景、旁白、时长和画面描述\n"
                    "2. 所有场景总时长应约等于 {{duration}} 秒\n"
                    "3. 旁白与视频标题一致\n\n"
                    "请生成完整视频脚本。"
                ),
                "variables_json": [
                    {
                        "name": "intent",
                        "type": "string",
                        "required": True,
                        "default": None,
                        "description": "视频意图",
                    },
                    {
                        "name": "duration",
                        "type": "integer",
                        "required": True,
                        "default": "30",
                        "description": "视频时长（秒）",
                    },
                    {
                        "name": "assets_text",
                        "type": "string",
                        "required": False,
                        "default": "无",
                        "description": "可用素材列表文本",
                    },
                ],
                "provider": "default",
                "is_active": 1,
            },
        ],
    )

    # Initial version 1 records so every seeded template has a tracked history.
    prompt_versions = sa.table(
        "prompt_versions",
        sa.column("id", sa.String()),
        sa.column("template_id", sa.String()),
        sa.column("version_no", sa.Integer()),
        sa.column("template_text", sa.Text()),
        sa.column("variables_json", sa.JSON()),
        sa.column("change_note", sa.Text()),
    )
    op.bulk_insert(
        prompt_versions,
        [
            {
                "id": f"pv-{template_id}",
                "template_id": template_id,
                "version_no": 1,
                "template_text": template_text,
                "variables_json": variables_json,
                "change_note": "从 pixelle_video/prompts/ 模块导入的初始版本",
            }
            for template_id, template_text, variables_json in [
                (
                    "pt-title-generation",
                    (
                        "请为以下内容生成一个简短、有吸引力的标题。\n\n内容：\n{{content}}\n\n"
                        "要求：\n1. 标题必须与输入内容使用相同语言\n"
                        "2. 标题长度不得超过 {{max_length}} 个字符\n3. 必须抓住内容的核心要点\n"
                        "4. 结尾不能带标点符号\n5. 输出完整有意义的短语\n\n标题："
                    ),
                    [
                        {
                            "name": "content",
                            "type": "string",
                            "required": True,
                            "default": None,
                            "description": "用于生成标题的内容",
                        },
                        {
                            "name": "max_length",
                            "type": "integer",
                            "required": False,
                            "default": "15",
                            "description": "标题最大字符数",
                        },
                    ],
                ),
                (
                    "pt-content-narration",
                    (
                        "# 角色定义\n你是专业的视频旁白创作师。\n\n# 核心任务\n"
                        "用户将提供内容，你需要为 {{n_storyboard}} 个视频分镜提取旁白。\n\n"
                        "# 输入内容\n{{content}}\n\n# 输出要求\n"
                        "1. 每段旁白字数严格控制为 {{min_words}}~{{max_words}} 词\n"
                        "2. 共输出 {{n_storyboard}} 段旁白\n"
                        '3. 严格按 JSON 格式输出 {"narrations": [旁白数组]}\n'
                        "4. 只输出 JSON\n\n现在，请从上述内容中提取 {{n_storyboard}} 段分镜旁白。"
                    ),
                    [
                        {
                            "name": "content",
                            "type": "string",
                            "required": True,
                            "default": None,
                            "description": "用户输入内容",
                        },
                        {
                            "name": "n_storyboard",
                            "type": "integer",
                            "required": True,
                            "default": "3",
                            "description": "分镜数量",
                        },
                        {
                            "name": "min_words",
                            "type": "integer",
                            "required": True,
                            "default": "50",
                            "description": "每段最少字数",
                        },
                        {
                            "name": "max_words",
                            "type": "integer",
                            "required": True,
                            "default": "100",
                            "description": "每段最多字数",
                        },
                    ],
                ),
                (
                    "pt-topic-narration",
                    (
                        "# 角色定义\n你是专业的视频旁白创作师。\n\n# 核心任务\n"
                        "用户将输入一个主题，你需要为这个主题创建 {{n_storyboard}} 个视频分镜旁白。\n\n"
                        "# 输入主题\n{{topic}}\n\n# 输出要求\n"
                        "1. 每段旁白字数严格控制为 {{min_words}}~{{max_words}} 词\n"
                        "2. 共输出 {{n_storyboard}} 段旁白\n"
                        '3. 严格按 JSON 格式输出 {"narrations": [旁白数组]}\n'
                        "4. 只输出 JSON\n\n现在，请为主题创建 {{n_storyboard}} 个分镜的旁白。"
                    ),
                    [
                        {
                            "name": "topic",
                            "type": "string",
                            "required": True,
                            "default": None,
                            "description": "视频主题",
                        },
                        {
                            "name": "n_storyboard",
                            "type": "integer",
                            "required": True,
                            "default": "3",
                            "description": "分镜数量",
                        },
                        {
                            "name": "min_words",
                            "type": "integer",
                            "required": True,
                            "default": "50",
                            "description": "每段最少字数",
                        },
                        {
                            "name": "max_words",
                            "type": "integer",
                            "required": True,
                            "default": "100",
                            "description": "每段最多字数",
                        },
                    ],
                ),
                (
                    "pt-video-generation",
                    (
                        "# 角色定义\n你是专业的视频创意设计师。\n\n# 核心任务\n"
                        "基于现有视频脚本，为每个分镜创建对应的**英文**视频生成提示词。\n\n"
                        "**重要：输入包含 {{narrations_count}} 条旁白，你必须为每条旁白生成一个视频提示词。**\n\n"
                        "# 输入内容\n{{narrations_json}}\n\n# 输出要求\n"
                        "1. 语言：必须使用英文\n2. 描述结构：场景 + 角色动作 + 镜头运动 + 情绪 + 氛围\n"
                        "3. 描述长度：建议 50-100 英文词\n\n# 输出格式\n"
                        '严格按 JSON 格式输出：{"video_prompts": ["英文视频提示词"]}\n\n'
                        "现在，请为上述 {{narrations_count}} 条旁白创建对应的**英文**视频提示词。只输出 JSON。"
                    ),
                    [
                        {
                            "name": "narrations_json",
                            "type": "json",
                            "required": True,
                            "default": None,
                            "description": "旁白数组 JSON",
                        },
                        {
                            "name": "narrations_count",
                            "type": "integer",
                            "required": True,
                            "default": None,
                            "description": "旁白数量",
                        },
                    ],
                ),
                (
                    "pt-image-generation",
                    (
                        "# 角色定义\n你是专业的图片创意设计师。\n\n# 核心任务\n"
                        "基于现有视频脚本，为每个分镜创建对应的**英文**图片提示词。\n\n"
                        "**重要：输入包含 {{narrations_count}} 条旁白，你必须为每条旁白生成一个图片提示词。**\n\n"
                        "# 输入内容\n{{narrations_json}}\n\n# 输出要求\n"
                        "1. 语言：必须使用英文\n2. 描述结构：主体 + 场景 + 构图 + 光线 + 风格\n\n"
                        "# 输出格式\n"
                        '严格按 JSON 格式输出：{"image_prompts": ["英文图片提示词"]}\n\n'
                        "现在，请为上述 {{narrations_count}} 条旁白创建对应的**英文**图片提示词。只输出 JSON。"
                    ),
                    [
                        {
                            "name": "narrations_json",
                            "type": "json",
                            "required": True,
                            "default": None,
                            "description": "旁白数组 JSON",
                        },
                        {
                            "name": "narrations_count",
                            "type": "integer",
                            "required": True,
                            "default": None,
                            "description": "旁白数量",
                        },
                    ],
                ),
                (
                    "pt-style-conversion",
                    (
                        "请将以下风格描述转换为 Stable Diffusion/FLUX 的详细图片生成提示词：\n\n"
                        "风格描述：{{description}}\n\n要求：\n1. 保留风格的核心特征\n"
                        "2. 补充光线、构图、材质等细节\n3. 输出英文提示词\n\n提示词："
                    ),
                    [
                        {
                            "name": "description",
                            "type": "string",
                            "required": True,
                            "default": None,
                            "description": "风格描述",
                        },
                    ],
                ),
                (
                    "pt-asset-script-generation",
                    (
                        "你是专业的视频脚本创作者。基于用户的视频意图和可用素材，生成一个 "
                        "{{duration}} 秒的视频脚本。检测用户的输入语言，如果是英文则所有文案必须使用英文。\n\n"
                        "# 输入\n- 视频意图：{{intent}}\n- 目标时长：{{duration}} 秒\n"
                        "- 可用素材：\n{{assets_text}}\n\n# 输出要求\n"
                        "1. 脚本包含场景、旁白、时长和画面描述\n2. 所有场景总时长应约等于 {{duration}} 秒\n\n"
                        "请生成完整视频脚本。"
                    ),
                    [
                        {
                            "name": "intent",
                            "type": "string",
                            "required": True,
                            "default": None,
                            "description": "视频意图",
                        },
                        {
                            "name": "duration",
                            "type": "integer",
                            "required": True,
                            "default": "30",
                            "description": "视频时长（秒）",
                        },
                        {
                            "name": "assets_text",
                            "type": "string",
                            "required": False,
                            "default": "无",
                            "description": "可用素材列表文本",
                        },
                    ],
                ),
            ]
        ],
    )

    # --- P4 seed: tag vocabulary (>= 20 tags) --------------------------------
    prompt_tags = sa.table(
        "prompt_tags",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
    )
    op.bulk_insert(
        prompt_tags,
        [
            {"id": f"tag-{index:02d}", "name": name}
            for index, name in enumerate(
                [
                    # 内容类型
                    "商品图",
                    "短视频",
                    "动画",
                    "字幕",
                    "旁白",
                    "分镜",
                    "标题",
                    # 镜头类型
                    "特写",
                    "中景",
                    "全景",
                    "航拍",
                    # 风格
                    "中式",
                    "日系",
                    "现代",
                    "复古",
                    # 平台
                    "TikTok",
                    "YouTube",
                    "Etsy",
                    "Instagram",
                    # 扩展标签
                    "产品展示",
                    "竖屏",
                ],
                start=1,
            )
        ],
    )

    # Bind a few representative tag relations to seeded templates.
    prompt_template_tags = sa.table(
        "prompt_template_tags",
        sa.column("template_id", sa.String()),
        sa.column("tag_id", sa.String()),
    )
    op.bulk_insert(
        prompt_template_tags,
        [
            {"template_id": "pt-title-generation", "tag_id": "tag-07"},
            {"template_id": "pt-title-generation", "tag_id": "tag-15"},
            {"template_id": "pt-video-generation", "tag_id": "tag-02"},
            {"template_id": "pt-image-generation", "tag_id": "tag-01"},
            {"template_id": "pt-style-conversion", "tag_id": "tag-12"},
        ],
    )


def downgrade() -> None:
    op.drop_table("prompt_template_tags")
    op.drop_table("prompt_tags")
    op.drop_table("prompt_versions")
    op.drop_table("prompt_templates")
