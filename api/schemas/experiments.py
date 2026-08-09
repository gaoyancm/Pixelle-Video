"""Strict public contracts for the phase 04-C experiment API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ExperimentMetric = Literal["cost", "quality", "composite"]
ExperimentStatus = Literal["active", "completed", "archived"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExperimentGroupCreate(StrictModel):
    group_name: str = Field(min_length=1, max_length=128)
    prompt_version_id: str | None = Field(default=None, max_length=64)
    model_name: str | None = Field(default=None, max_length=64)
    params_json: dict[str, Any] | None = None


class ExperimentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    metric: ExperimentMetric = "composite"
    groups: list[ExperimentGroupCreate] = Field(default_factory=list, max_length=16)


class ExperimentResponse(StrictModel):
    id: str
    name: str
    description: str | None
    metric: str
    status: str
    created_at: datetime


class ExperimentList(StrictModel):
    items: list[ExperimentResponse]
    has_more: bool


class ExperimentGroupDetail(StrictModel):
    id: str
    group_name: str
    prompt_version_id: str | None
    model_name: str | None
    params_json: dict[str, Any] | None
    created_at: datetime


class ExperimentDetail(ExperimentResponse):
    groups: list[ExperimentGroupDetail]


class ExperimentUpdate(StrictModel):
    status: ExperimentStatus


class JobMetricsPayload(StrictModel):
    job_id: str
    group_id: str
    group_name: str
    duration_seconds: float | None
    cost: float | None
    qc_score: float | None
    size_bytes: int | None
    composite: float


class GroupMetricsPayload(StrictModel):
    group_id: str
    group_name: str
    prompt_version_id: str | None = None
    sample_size: int
    jobs: list[JobMetricsPayload]


class ExperimentMetricsResponse(StrictModel):
    experiment_id: str
    computed_at: str
    groups: list[GroupMetricsPayload]


class ExperimentJobEntry(StrictModel):
    job_id: str
    group_id: str
    group_name: str
    created_at: datetime


class ExperimentJobsResponse(StrictModel):
    experiment_id: str
    items: list[ExperimentJobEntry]


class ReportGroup(StrictModel):
    group_id: str
    name: str
    sample_size: int
    mean_score: float
    median_score: float
    std: float


class ExperimentReportResponse(StrictModel):
    experiment_id: str
    metric: str
    groups: list[ReportGroup]
    winner: str | None
    confidence: str
    improvement: float | None


class FailureSampleResponse(StrictModel):
    id: str
    job_id: str
    prompt_version_id: str | None
    reason: str | None
    qc_issues: list[dict[str, Any]] | None
    created_at: datetime


class FailureList(StrictModel):
    items: list[FailureSampleResponse]
