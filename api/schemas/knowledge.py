"""Strict public contracts for the phase 04-D knowledge base API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KnowledgeCategory = Literal[
    "策划", "平台规则", "镜头叙事", "角色场景", "模型工作流", "品牌产品", "后处理", "故障诊断"
]
EvidenceClass = Literal["documented_fact", "empirical_observation", "production_heuristic"]
EntryStatus = Literal["draft", "published", "archived"]
LinkTargetType = Literal["prompt_template", "qc_rule", "workflow"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeEntryCreate(StrictModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=100_000)
    category: KnowledgeCategory
    evidence_class: EvidenceClass
    status: EntryStatus = "draft"
    source_url: str | None = Field(default=None, max_length=1024)
    source_doc: str | None = Field(default=None, max_length=10_000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeEntryUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    category: KnowledgeCategory | None = None
    evidence_class: EvidenceClass | None = None
    status: EntryStatus | None = None
    source_url: str | None = Field(default=None, max_length=1024)
    source_doc: str | None = Field(default=None, max_length=10_000)


class KnowledgeEntryResponse(StrictModel):
    id: str
    title: str
    content: str
    category: str
    evidence_class: str
    status: str
    source_url: str | None
    source_doc: str | None
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
    stale: bool
    tags: list[str]


class KnowledgeEntryList(StrictModel):
    items: list[KnowledgeEntryResponse]
    has_more: bool


class KnowledgeLinkCreate(StrictModel):
    target_type: LinkTargetType
    target_id: str = Field(min_length=1, max_length=64)
    link_note: str | None = Field(default=None, max_length=10_000)


class KnowledgeLinkResponse(StrictModel):
    id: str
    entry_id: str
    target_type: str
    target_id: str
    link_note: str | None
    created_at: datetime


class KnowledgeLinkList(StrictModel):
    items: list[KnowledgeLinkResponse]
