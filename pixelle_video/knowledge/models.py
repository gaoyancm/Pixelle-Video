"""SQLAlchemy persistence models for the phase 04-D knowledge base."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pixelle_video.media_jobs.models import Base, UTCDateTime, utc_now

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
LINK_TARGET_TYPES = ("prompt_template", "qc_rule", "workflow")
STALE_AFTER_DAYS = 180


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        CheckConstraint(
            "category IN ('策划', '平台规则', '镜头叙事', '角色场景', '模型工作流', '品牌产品', '后处理', '故障诊断')",
            name="ck_knowledge_entries_category",
        ),
        CheckConstraint(
            "evidence_class IN ('documented_fact', 'empirical_observation', 'production_heuristic')",
            name="ck_knowledge_entries_evidence_class",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_knowledge_entries_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_class: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="production_heuristic",
        server_default=text("'production_heuristic'"),
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    source_url: Mapped[str | None] = mapped_column(String(1024))
    source_doc: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class KnowledgeTag(Base):
    __tablename__ = "knowledge_tags"
    __table_args__ = (UniqueConstraint("name", name="uq_knowledge_tags_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class KnowledgeEntryTag(Base):
    __tablename__ = "knowledge_entry_tags"

    entry_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_entries.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_tags.id", ondelete="CASCADE"), primary_key=True
    )


class KnowledgeLink(Base):
    __tablename__ = "knowledge_links"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('prompt_template', 'qc_rule', 'workflow')",
            name="ck_knowledge_links_target_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entry_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_entries.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    link_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
