"""Public contracts for phase 03-F audit and budget endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditEventItem(StrictModel):
    event_id: str
    event_type: str
    scope_type: str
    scope_id: str
    operator: str
    details: dict[str, Any] | None = None
    cost_snapshot: dict[str, Any] | None = None
    created_at: datetime


class AuditListResponse(StrictModel):
    items: list[AuditEventItem]
    limit: int
    offset: int
    has_more: bool


BudgetMode = Literal["observe", "warn", "cap"]


class BudgetConfigResponse(StrictModel):
    per_task_limit: float | None
    per_batch_limit: float | None
    mode: BudgetMode


class BudgetConfigUpdate(StrictModel):
    per_task_limit: float | None = Field(default=None, ge=0)
    per_batch_limit: float | None = Field(default=None, ge=0)
    mode: BudgetMode = "observe"


class BudgetUsageResponse(StrictModel):
    project_id: str
    job_count: int
    estimated_total: float
    actual_total: float
    per_task_limit: float | None
    per_batch_limit: float | None
    mode: BudgetMode


class ApprovalRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2000)
