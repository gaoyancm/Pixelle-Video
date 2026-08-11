"""Strict public contracts for the phase 04-E orchestration API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PlanStatus = Literal[
    "draft", "generating", "awaiting_approval", "approved", "rejected", "completed", "stage_failed"
]
Intent = Literal["product_ad", "short_video", "animation", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanCreateRequest(StrictModel):
    request_text: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = Field(default=None, max_length=64)


class PlanResponse(StrictModel):
    id: str
    project_id: str | None
    request_text: str
    intent: str
    plan_json: dict[str, Any]
    status: str
    cost_estimate: float | None
    checkpoint_json: dict[str, Any] | None
    created_at: datetime


class GenerateResponse(StrictModel):
    plan_id: str
    status: str
    checkpoint: dict[str, Any]


class StatusResponse(StrictModel):
    plan_id: str
    status: str
    current_stage: str | None
    completed_stages: list[str]
    total_cost_so_far: float


class ApproveResponse(StrictModel):
    plan_id: str
    status: str


class RejectRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


class RetryStageRequest(StrictModel):
    stage: str = Field(min_length=1, max_length=64)


class ApprovalSummaryResponse(StrictModel):
    plan_id: str
    summary: str
    cost_estimate: float | None
    grade: str | None
    issues: list[dict[str, Any]]
    details: dict[str, Any]
