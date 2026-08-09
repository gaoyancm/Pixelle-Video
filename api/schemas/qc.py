"""Strict public contracts for the phase 04-B QC pipeline API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

QCCategory = Literal["technical", "visual", "character", "narrative", "brand", "platform"]
QCRuleType = Literal["schema_validation", "parameter_check", "text_analysis"]
QCSeverity = Literal["critical", "major", "minor"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QCRuleCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    category: QCCategory
    rule_type: QCRuleType
    rule_config: dict[str, Any] = Field(default_factory=dict)
    provider: str = Field(default="local", min_length=1, max_length=32)
    priority: int = Field(default=1, ge=1, le=1000)


class QCRuleUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: QCCategory | None = None
    rule_type: QCRuleType | None = None
    rule_config: dict[str, Any] | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    is_active: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)


class QCRuleResponse(StrictModel):
    id: str
    name: str
    category: str
    rule_type: str
    rule_config: dict[str, Any]
    provider: str
    is_active: bool
    priority: int
    created_at: datetime
    updated_at: datetime


class QCRuleList(StrictModel):
    items: list[QCRuleResponse]


class QCProfileCreate(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=5000)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    is_default: bool = False


class QCProfileResponse(StrictModel):
    id: str
    name: str
    description: str | None
    rules: list[dict[str, Any]]
    is_default: bool
    created_at: datetime


class QCProfileList(StrictModel):
    items: list[QCProfileResponse]


class QCIssue(StrictModel):
    rule_id: str
    severity: str
    category: str
    field: str
    expected: Any
    actual: Any
    message: str


class QCResultResponse(StrictModel):
    job_id: str
    executed_at: datetime
    profile: str
    total_rules: int
    passed: int
    issues: list[QCIssue]
    decision: str
    suggestions: list[str]


class QCDiagnoseRequest(StrictModel):
    issue_type: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(default_factory=dict)


class QCDiagnosisResponse(StrictModel):
    issue_type: str
    symptom: str
    root_cause: str
    fix: str


class QCReportResponse(StrictModel):
    job_id: str
    profile: str
    decision: str
    summary: str
    passed: int
    total_rules: int
    issues: list[QCIssue]
    diagnostics: list[QCDiagnosisResponse]
    report_text: str
