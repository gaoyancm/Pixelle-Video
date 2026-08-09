"""Seed the phase 04-D knowledge base with >= 20 initial entries.

Revision ID: 0010_seed_knowledge
Revises: 0009_add_knowledge
Create Date: 2026-08-09

Data-only migration (no schema change). Idempotent: rows are inserted only
when the title is not already present.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_seed_knowledge"
down_revision: Union[str, Sequence[str], None] = "0009_add_knowledge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TAGS = [
    "TikTok",
    "视频",
    "文案",
    "竖屏",
    "Hook",
    "A800",
    "Wan2.2",
    "QC",
    "Prompt",
    "品牌",
    "镜头",
    "故障",
]

ENTRIES = [
    # --- documented_fact: survey reports & reference projects (>= 5) ---------
    {
        "title": "TikTok短视频开头Hook设计原则",
        "content": "前 1-2 秒必须抓住注意力：问题开场、反转、强悬念或直接利益点；\n\n"
        "参考项目勘测结论：Hook 决定完播率，前 3 秒流失超过 50% 的观众。",
        "category": "策划",
        "evidence_class": "documented_fact",
        "status": "published",
        "source_doc": "参考项目勘测报告（TikTok 内容分析）",
        "tags": ["TikTok", "文案", "Hook"],
    },
    {
        "title": "脚本三幕式结构",
        "content": "开头（Hook）→ 中间（核心信息/展示）→ 结尾（CTA/记忆点）。\n\n"
        "短视频脚本遵循 15-60 秒时长内的三幕压缩结构。",
        "category": "策划",
        "evidence_class": "documented_fact",
        "status": "published",
        "source_doc": "参考项目勘测报告（脚本结构分析）",
        "tags": ["文案", "视频"],
    },
    {
        "title": "TikTok 视频规格",
        "content": "推荐 9:16 竖屏 1080x1920，时长 15-60 秒，码率不低于 4Mbps；"
        "封面文字不超过画面 1/3。",
        "category": "平台规则",
        "evidence_class": "documented_fact",
        "status": "published",
        "source_url": "https://creators.tiktok.com/",
        "tags": ["TikTok", "竖屏"],
    },
    {
        "title": "YouTube Shorts 规格",
        "content": "竖屏 9:16，最长 60 秒，分辨率 1080x1920；标题含关键词利于搜索推荐。",
        "category": "平台规则",
        "evidence_class": "documented_fact",
        "status": "published",
        "source_doc": "参考项目勘测报告（平台规格）",
        "tags": ["视频", "竖屏"],
    },
    {
        "title": "Wan2.2 T2V 模型参数限制",
        "content": "Wan2.2 文生视频 33f 变体：分辨率上限 1280x720（33f 场景），"
        "单次生成时长受显存限制；A800 上实测批次上限受显存约束。",
        "category": "模型工作流",
        "evidence_class": "documented_fact",
        "status": "published",
        "source_doc": "参考项目勘测报告（Wan2.2 参数）",
        "tags": ["Wan2.2", "A800"],
    },
    {
        "title": "产品信息一致性要求",
        "content": "品牌内容中产品名称、价格、卖点必须与 Brief 完全一致，"
        "禁止自由发挥改写产品参数。",
        "category": "品牌产品",
        "evidence_class": "documented_fact",
        "status": "published",
        "source_doc": "参考项目勘测报告（品牌合规）",
        "tags": ["品牌", "QC"],
    },
    # --- production_heuristic: prompts modules & V2.6 appendix (>= 5) --------
    {
        "title": "受众匹配启发式",
        "content": "生成前先定义目标受众，Prompt 中显式包含受众偏好关键词，可显著提升点击匹配度。",
        "category": "策划",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["文案", "Prompt"],
    },
    {
        "title": "景别切换节奏",
        "content": "叙事镜头建议远-中-近景循环，每 3-5 秒切换一次景别保持节奏；"
        "商品展示场景多用近景特写。",
        "category": "镜头叙事",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "pixelle_video/prompts 使用经验",
        "tags": ["镜头", "视频"],
    },
    {
        "title": "转场技巧",
        "content": "硬切用于快节奏，叠化/缩放转场用于情绪过渡；避免连续硬切超过 3 次。",
        "category": "镜头叙事",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "pixelle_video/prompts 使用经验",
        "tags": ["镜头"],
    },
    {
        "title": "角色外貌一致性描述",
        "content": "同一角色跨镜头必须复用完全一致的描述词（发型/服装/肤色），并放入身份锚点字段。",
        "category": "角色场景",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "pixelle_video/prompts 使用经验",
        "tags": ["Prompt", "镜头"],
    },
    {
        "title": "世界观设定要点",
        "content": "场景描述需包含时间、地点、光线、氛围四个要素，世界观越具体生成一致性越高。",
        "category": "角色场景",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["Prompt"],
    },
    {
        "title": "模型成本对比最佳实践",
        "content": "同任务优先用低成本模型试跑，效果不达标再升级高成本模型；成本差异可达 5-10 倍。",
        "category": "模型工作流",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["Wan2.2"],
    },
    {
        "title": "参数最佳实践",
        "content": "温度 0.7-0.9 区间适合创意文案，0.3-0.5 适合事实型内容；"
        "步数越多细节越好但耗时线性增长。",
        "category": "模型工作流",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "pixelle_video/prompts 使用经验",
        "tags": ["Prompt", "Wan2.2"],
    },
    {
        "title": "品牌 Logo 配色字体语调",
        "content": "品牌内容必须使用品牌色 hex 与指定字体；语调保持品牌既定语气，禁用夸张宣传语。",
        "category": "品牌产品",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["品牌", "Prompt"],
    },
    {
        "title": "字幕样式规范",
        "content": "字幕字号不低于画面宽度 1/15，对比度优先浅底深字或深底浅字；"
        "每行不超过 12 个汉字。",
        "category": "后处理",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "pixelle_video/prompts 使用经验",
        "tags": ["视频"],
    },
    {
        "title": "BGM 匹配原则",
        "content": "BGM 节奏与剪辑节奏匹配，广告素材音量压到人声 60% 以下；"
        "避免使用有版权争议热门曲目。",
        "category": "后处理",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["视频"],
    },
    {
        "title": "平台禁用项政策",
        "content": "生成内容禁止含 NSFW、暴力、政治敏感与版权风险素材；"
        "平台规则类知识需随政策更新。",
        "category": "平台规则",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["QC", "TikTok"],
    },
    {
        "title": "常见失败原因速查",
        "content": "生成失败三大原因：参数越界、显存不足、Prompt 违反模型约束；"
        "先查这三项再排查网络。",
        "category": "故障诊断",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "V2.6 附录 A.2 知识库定义",
        "tags": ["故障", "QC"],
    },
    # --- empirical_observation: phase 02 A800 real runs (>= 3) ---------------
    {
        "title": "A800 WAN 33f 真实验证经验",
        "content": "阶段 02 真机验证：WAN 33f 变体在 A800 上 720p 单条生成耗时约 "
        "2-4 分钟；批量提交必须控制并发避免 OOM。",
        "category": "模型工作流",
        "evidence_class": "empirical_observation",
        "status": "published",
        "source_doc": "阶段 02 A800 真实验证记录",
        "tags": ["A800", "Wan2.2"],
    },
    {
        "title": "A800 批次处理实测",
        "content": "同一显存下并发 2 条 720p 任务为安全上限；超过后显存耗尽导致 节点重启。",
        "category": "模型工作流",
        "evidence_class": "empirical_observation",
        "status": "published",
        "source_doc": "阶段 02 A800 真实验证记录",
        "tags": ["A800"],
    },
    {
        "title": "分辨率越界失败模式",
        "content": "实测：请求 1080x1920 超过模型约束时任务静默失败；先校验 workflow 参数再提交。",
        "category": "故障诊断",
        "evidence_class": "empirical_observation",
        "status": "published",
        "source_doc": "阶段 02 A800 真实验证记录",
        "tags": ["故障", "A800"],
    },
    {
        "title": "超时重试成功模式",
        "content": "实测：网络抖动导致的提交超时，重试 2-3 次可恢复；重试需保证 幂等避免重复扣费。",
        "category": "故障诊断",
        "evidence_class": "empirical_observation",
        "status": "published",
        "source_doc": "阶段 02 A800 真实验证记录",
        "tags": ["故障"],
    },
    # --- design borrowings list (>= 2) ---------------------------------------
    {
        "title": "OpenMontage 审查元技能借鉴",
        "content": "审查采用 critical/suggestion/nitpick 三级 + 最多 2 轮修复；"
        "应用于 QC 决策与代码审查流程。",
        "category": "策划",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "参考项目设计借鉴清单",
        "tags": ["QC"],
    },
    {
        "title": "GMS 验收矩阵维度借鉴",
        "content": "验收矩阵 7 维度 + Critical/Major/Minor 分级；应用于 QC 规则 "
        "严重性设计与种子数据导入。",
        "category": "品牌产品",
        "evidence_class": "production_heuristic",
        "status": "published",
        "source_doc": "参考项目设计借鉴清单",
        "tags": ["QC", "品牌"],
    },
]


def upgrade() -> None:
    knowledge_tags = sa.table(
        "knowledge_tags",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
    )
    knowledge_entries = sa.table(
        "knowledge_entries",
        sa.column("id", sa.String()),
        sa.column("title", sa.String()),
        sa.column("content", sa.Text()),
        sa.column("category", sa.String()),
        sa.column("evidence_class", sa.String()),
        sa.column("status", sa.String()),
        sa.column("source_url", sa.String()),
        sa.column("source_doc", sa.Text()),
    )
    knowledge_entry_tags = sa.table(
        "knowledge_entry_tags",
        sa.column("entry_id", sa.String()),
        sa.column("tag_id", sa.String()),
    )

    connection = op.get_bind()

    # Idempotent tag seeding.
    tag_ids: dict[str, str] = {}
    existing_tags = set(connection.execute(sa.select(knowledge_tags.c.name)).scalars())
    new_tags = [
        {"id": f"kt-{tag.lower()}", "name": tag} for tag in TAGS if tag not in existing_tags
    ]
    if new_tags:
        op.bulk_insert(knowledge_tags, new_tags)
    for tag in TAGS:
        tag_ids[tag] = connection.execute(
            sa.select(knowledge_tags.c.id).where(knowledge_tags.c.name == tag)
        ).scalar_one()

    # Idempotent entry seeding (skip titles that already exist).
    existing_titles = set(connection.execute(sa.select(knowledge_entries.c.title)).scalars())
    new_entries = [
        {
            "id": f"ke-{index:02d}",
            "title": entry["title"],
            "content": entry["content"],
            "category": entry["category"],
            "evidence_class": entry["evidence_class"],
            "status": entry["status"],
            "source_url": entry.get("source_url"),
            "source_doc": entry.get("source_doc"),
        }
        for index, entry in enumerate(ENTRIES)
        if entry["title"] not in existing_titles
    ]
    if new_entries:
        op.bulk_insert(knowledge_entries, new_entries)
    entry_tags = [
        {"entry_id": f"ke-{index:02d}", "tag_id": tag_ids[tag]}
        for index, entry in enumerate(ENTRIES)
        if entry["title"] not in existing_titles
        for tag in entry.get("tags", [])
    ]
    if entry_tags:
        op.bulk_insert(knowledge_entry_tags, entry_tags)


def downgrade() -> None:
    connection = op.get_bind()
    titles = [entry["title"] for entry in ENTRIES]
    knowledge_entries = sa.table("knowledge_entries", sa.column("title", sa.String()))
    connection.execute(knowledge_entries.delete().where(knowledge_entries.c.title.in_(titles)))
