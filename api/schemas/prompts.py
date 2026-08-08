"""Strict public contracts for the phase 04-A prompt template API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VariableType = Literal["string", "integer", "json"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VariableDef(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    type: VariableType = "string"
    required: bool = False
    default: str | None = None
    description: str | None = Field(default=None, max_length=512)


class PromptCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=64)
    template_text: str = Field(min_length=1)
    description: str | None = Field(default=None, max_length=10_000)
    variables: list[VariableDef] = Field(default_factory=list, max_length=64)
    provider: str = Field(default="default", min_length=1, max_length=32)
    change_note: str | None = Field(default=None, max_length=2000)


class PromptUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    template_text: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=10_000)
    variables: list[VariableDef] | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    is_active: bool | None = None
    change_note: str | None = Field(default=None, max_length=2000)


class PromptResponse(StrictModel):
    id: str
    name: str
    category: str
    description: str | None
    template_text: str
    variables: list[VariableDef]
    provider: str
    is_active: bool
    current_score: int | None
    usage_count: int
    last_used_at: datetime | None
    archived: bool
    created_at: datetime
    updated_at: datetime


class PromptList(StrictModel):
    items: list[PromptResponse]
    limit: int
    offset: int
    has_more: bool


class CompileRequest(StrictModel):
    variables: dict[str, Any] = Field(default_factory=dict)


class CompileResponse(StrictModel):
    id: str
    name: str
    prompt: str


class ValidateResponse(StrictModel):
    id: str
    valid: bool
    issues: list[str]


class VersionResponse(StrictModel):
    id: str
    version_no: int
    template_text: str
    variables: list[VariableDef]
    change_note: str | None
    created_at: datetime


class VersionList(StrictModel):
    template_id: str
    items: list[VersionResponse]


class RollbackRequest(StrictModel):
    change_note: str | None = Field(default=None, max_length=2000)


class RateRequest(StrictModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class TagResponse(StrictModel):
    id: str
    name: str


class TagList(StrictModel):
    items: list[TagResponse]


class BindTagsRequest(StrictModel):
    tag_ids: list[str] = Field(min_length=1, max_length=32)


class AntiSlopViolation(StrictModel):
    word: str
    category: str
    category_label: str
    suggestion: str


class AntiSlopResponse(StrictModel):
    slop_count: int
    density: float
    violations: list[AntiSlopViolation]
