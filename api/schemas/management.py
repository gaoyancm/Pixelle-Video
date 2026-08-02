"""Strict public contracts for the phase 03 management API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PriorityName = Literal["low", "normal", "high"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)


class ProjectUpdate(ProjectCreate):
    pass


class ProjectResponse(StrictModel):
    project_id: str
    name: str
    description: str | None
    archived: bool
    created_at: datetime
    updated_at: datetime


class ProjectList(StrictModel):
    items: list[ProjectResponse]
    limit: int
    offset: int
    has_more: bool


class BatchCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    workflow: str = Field(min_length=1, max_length=128)
    common_parameters: dict[str, Any] = Field(default_factory=dict)
    default_priority: PriorityName = "normal"


class BatchUpdate(BatchCreate):
    expected_batch_version: int = Field(ge=1)


class BatchResponse(StrictModel):
    batch_id: str
    project_id: str
    name: str
    workflow: str
    state: Literal["draft", "submitted"]
    common_parameters: dict[str, Any]
    default_priority: PriorityName
    item_limit_snapshot: int | None
    version: int
    archived: bool
    created_at: datetime
    updated_at: datetime
    items: list[DraftItemResponse] | None = None


class BatchList(StrictModel):
    items: list[BatchResponse]
    limit: int
    offset: int
    has_more: bool


class DraftAssetInput(StrictModel):
    asset_id: str = Field(min_length=1, max_length=128)
    role: str = Field(default="input_image", min_length=1, max_length=64)
    position: int = Field(default=0, ge=0)


class DraftItemInput(StrictModel):
    item_id: str | None = Field(default=None, min_length=1, max_length=36)
    position: int = Field(ge=0)
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    priority_override: PriorityName | None = None
    assets: list[DraftAssetInput] = Field(default_factory=list, max_length=1)


class DraftItemsReplace(StrictModel):
    expected_batch_version: int = Field(ge=1)
    items: list[DraftItemInput] = Field(max_length=100)

    @field_validator("items")
    @classmethod
    def positions_are_unique(cls, value: list[DraftItemInput]) -> list[DraftItemInput]:
        positions = [item.position for item in value]
        if len(positions) != len(set(positions)):
            raise ValueError("item positions must be unique")
        return value


class DraftAssetResponse(StrictModel):
    asset_id: str
    role: str
    position: int


class DraftItemResponse(StrictModel):
    item_id: str
    position: int
    parameter_overrides: dict[str, Any]
    priority_override: PriorityName | None
    assets: list[DraftAssetResponse]


class DraftItemsResponse(StrictModel):
    batch_id: str
    batch_version: int
    items: list[DraftItemResponse]


class VersionRequest(StrictModel):
    expected_batch_version: int = Field(ge=1)


class BatchPriorityUpdate(VersionRequest):
    priority: PriorityName


class ItemPriorityUpdate(VersionRequest):
    priority: PriorityName | None


class BatchPriorityResponse(StrictModel):
    batch_id: str
    batch_version: int
    default_priority: PriorityName
    updated: int
    unchanged: int
    overridden: int
    skipped_claimed_or_nonqueued: int


class ItemPriorityResponse(StrictModel):
    batch_id: str
    batch_version: int
    item_id: str
    priority_override: PriorityName | None
    effective_priority: PriorityName
    job_priority_updated: bool


class OperationItem(StrictModel):
    item_id: str
    job_id: str | None
    disposition: str


class CancelResponse(StrictModel):
    batch_id: str
    batch_version: int
    operation_id: str
    message: str
    requested: int
    already_requested: int
    skipped_terminal: int
    not_found: int
    items: list[OperationItem]


class RetryItem(StrictModel):
    item_id: str
    source_job_id: str | None
    job_id: str | None
    source_attempt_no: int | None
    attempt_no: int | None
    disposition: Literal["retried", "skipped"]


class RetryResponse(StrictModel):
    batch_id: str
    batch_version: int
    operation_id: str
    retried: int
    skipped: int
    items: list[RetryItem]


class ProgressCounts(StrictModel):
    queued: int
    submitting: int
    running: int
    cancel_requested: int
    succeeded: int
    failed: int
    timed_out: int
    cancelled: int


class ProgressResponse(StrictModel):
    batch_id: str
    batch_state: Literal["draft", "submitted"]
    total_items: int
    completed_items: int
    derived_result: (
        Literal["all_succeeded", "partially_succeeded", "all_failed", "completed_with_cancellation"]
        | None
    )
    counts: ProgressCounts


class ResultError(StrictModel):
    code: str
    message: str


class ResultAsset(StrictModel):
    asset_id: str
    original_filename: str
    media_type: str
    mime_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    content_href: str
    preview_href: str


class ResultAttempt(StrictModel):
    attempt_no: int
    job_id: str
    workflow: str
    retry_of_attempt_no: int | None
    retry_of_job_id: str | None
    status: str
    effective_priority: PriorityName
    cancel_requested: bool
    can_retry: bool
    created_at: datetime
    updated_at: datetime
    error: ResultError | None
    outputs: list[ResultAsset]


class ResultItem(StrictModel):
    item_id: str
    position: int
    current_attempt_no: int | None
    attempts: list[ResultAttempt]


class ResultsResponse(StrictModel):
    batch_id: str
    items: list[ResultItem]


class PreflightIssue(StrictModel):
    code: str
    message: str
    item_id: str | None = None
    position: int | None = None
    field: str | None = None


class PreflightResponse(StrictModel):
    batch_id: str
    batch_version: int
    valid: bool
    item_count: int
    issues: list[PreflightIssue]


class SubmittedJob(StrictModel):
    item_id: str
    job_id: str
    status: Literal["queued"] = "queued"
    priority: PriorityName


class SubmitResponse(StrictModel):
    batch_id: str
    batch_version: int
    operation_id: str
    jobs: list[SubmittedJob]


class WorkflowParameter(StrictModel):
    name: str
    required: bool
    type: str
    constraints: dict[str, Any] = Field(default_factory=dict)


class WorkflowCatalogItem(StrictModel):
    workflow: str
    display_name: str
    mode: Literal["text_to_video", "image_to_video"]
    requires_image: bool
    input_asset_count: int
    available: bool
    unavailable_reason: str | None = None
    parameters: list[WorkflowParameter]


class WorkflowCatalog(StrictModel):
    items: list[WorkflowCatalogItem]
