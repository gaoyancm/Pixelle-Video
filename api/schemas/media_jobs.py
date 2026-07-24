"""Public, allow-listed schemas for the persistent media-job API."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pixelle_video.media_jobs.state_machine import JobStatus
from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~:-]{1,128}$")
_PARAMETER_LIMIT = 32
_PARAMETER_BYTES = 32_768
_ASSET_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

Prompt = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
NegativePrompt = Annotated[str, Field(strict=True, max_length=4096)]
Dimension = Annotated[int, Field(strict=True, ge=1, le=16_384)]
FrameCount = Annotated[int, Field(strict=True, ge=1, le=10_000)]
Seed = Annotated[int, Field(strict=True, ge=0, le=18_446_744_073_709_551_615)]
Steps = Annotated[int, Field(strict=True, ge=1, le=1_000)]
Cfg = Annotated[float, Field(strict=True, ge=0, le=100)]


class MediaJobParameters(BaseModel):
    """Central public parameter contract; workflow applicability comes from WORKFLOW_SPECS."""

    model_config = ConfigDict(extra="forbid")

    prompt: Prompt
    negative_prompt: NegativePrompt | None = None
    width: Dimension | None = None
    height: Dimension | None = None
    frame_count: FrameCount | None = None
    seed: Seed | None = None
    steps: Steps | None = None
    cfg: Cfg | None = None

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value

    @field_validator("cfg")
    @classmethod
    def cfg_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("cfg must be finite")
        return value


class MediaJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1, max_length=128)
    parameters: MediaJobParameters
    asset_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("workflow")
    @classmethod
    def workflow_must_be_registered(cls, value: str) -> str:
        if value not in WORKFLOW_SPECS:
            raise ValueError("workflow is not allowed")
        return value

    @field_validator("asset_id")
    @classmethod
    def asset_must_be_safe_id(cls, value: str | None) -> str | None:
        if value is not None and not _ASSET_ID.fullmatch(value):
            raise ValueError("asset_id must be a managed asset identifier")
        return value

    @model_validator(mode="after")
    def parameters_must_match_workflow(self):
        spec = WORKFLOW_SPECS[self.workflow]
        allowed = set(spec.parameter_targets) - {"input_image", "output_prefix"}
        supplied = self.parameters.model_dump(exclude_none=True)
        if len(supplied) > _PARAMETER_LIMIT:
            raise ValueError("too many workflow parameters")
        inapplicable = set(supplied) - allowed
        if inapplicable:
            raise ValueError("parameter is not applicable to this workflow")
        if len(json.dumps(supplied, ensure_ascii=False).encode("utf-8")) > _PARAMETER_BYTES:
            raise ValueError("workflow parameters are too large")
        if spec.requires_image and self.asset_id is None:
            raise ValueError("this workflow requires exactly one managed asset")
        if not spec.requires_image and self.asset_id is not None:
            raise ValueError("this workflow does not accept assets")
        return self

    def public_parameters(self) -> dict[str, Any]:
        return self.parameters.model_dump(exclude_none=True)


class MediaJobOutput(BaseModel):
    output_id: str
    media_type: str
    size: int
    mime_type: str
    sha256: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    frame_count: int | None = None


class MediaJobError(BaseModel):
    code: str
    message: str


class MediaJobResponse(BaseModel):
    job_id: str
    workflow: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    cancel_requested: bool
    retry_of_job_id: str | None = None
    outputs: list[MediaJobOutput] = Field(default_factory=list)
    error: MediaJobError | None = None


class MediaJobListResponse(BaseModel):
    items: list[MediaJobResponse]
    limit: int
    offset: int
    has_more: bool


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def validate_idempotency_key(value: str | None) -> str:
    if value is None or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError("Idempotency-Key must use 1-128 safe ASCII characters")
    return value
