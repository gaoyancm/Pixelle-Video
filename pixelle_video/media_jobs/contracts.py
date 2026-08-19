"""Validated, provider-independent media-job data contracts."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FORBIDDEN_SECRET_KEYS = {
    "apikey",
    "authorization",
    "accesstoken",
    "refreshtoken",
    "accesskey",
    "secretkey",
    "password",
    "databasepassword",
    "databaseurl",
    "comfyuibaseurl",
}

# Every kind a production producer may create must have a registered worker
# processor.  "comfyui" (the legacy image path) is deliberately excluded after
# the phase-10 task-2 migration onto the private_comfyui registry.
KNOWN_EXECUTOR_KINDS = frozenset({"private_comfyui", "llm_caption"})


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def reject_secret_fields(value: Any, *, path: str = "input") -> Any:
    """Reject credential-shaped field names before they reach persistent JSON."""

    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(str(key)) in _FORBIDDEN_SECRET_KEYS:
                raise ValueError(f"secret field is not allowed in persisted {path}: {key}")
            reject_secret_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_fields(child, path=f"{path}[{index}]")
    return value


_HASHED_REQUEST_FIELDS = (
    "workflow_type",
    "workflow_key",
    "executor_kind",
    "provider",
    "node_id",
    "input_json",
    "input_assets_json",
)


def compute_request_hash(payload: Mapping[str, Any]) -> str:
    """Hash only immutable request content using canonical UTF-8 JSON.

    Runtime identity, idempotency, submission, timestamps, retry counters, and
    status fields are deliberately excluded from the trusted hash boundary.
    """

    canonical_payload = {field: payload.get(field) for field in _HASHED_REQUEST_FIELDS}

    reject_secret_fields(canonical_payload)
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MediaInputAsset(BaseModel):
    """Stable reference to an asset registered by a later phase."""

    asset_id: str = Field(min_length=1, max_length=128)
    role: str | None = Field(default=None, max_length=64)


class MediaOutputMetadata(BaseModel):
    """Safe metadata for a platform-managed output file."""

    output_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=32)
    relative_path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    mime_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration: float | None = Field(default=None, ge=0)
    frame_count: int | None = Field(default=None, ge=0)
    content: str | None = Field(
        default=None, max_length=100_000, description="Inline text content for text outputs"
    )

    @field_validator("relative_path")
    @classmethod
    def path_must_be_managed_and_relative(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("relative_path must use forward slashes")
        posix_path = PurePosixPath(value)
        if posix_path.is_absolute() or PureWindowsPath(value).is_absolute():
            raise ValueError("relative_path must not be absolute")
        if any(part in {"", ".", ".."} for part in posix_path.parts):
            raise ValueError("relative_path must not contain traversal segments")
        if ":" in posix_path.parts[0]:
            raise ValueError("relative_path must not contain a drive prefix")
        return posix_path.as_posix()


class MediaJobCreate(BaseModel):
    """Fields accepted when creating a new queued media job."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=36, max_length=36)
    workflow_type: str = Field(min_length=1, max_length=128)
    workflow_key: str = Field(min_length=1, max_length=512)
    executor_kind: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    node_id: str | None = Field(default=None, max_length=128)
    input_json: dict[str, Any] = Field(default_factory=dict)
    input_assets_json: list[MediaInputAsset] = Field(default_factory=list)
    submission_token: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        min_length=1,
        max_length=128,
    )
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    deadline_at: datetime | None = None
    retry_of_job_id: str | None = Field(default=None, min_length=36, max_length=36)
    priority: int = Field(default=1, ge=0, le=2)

    def immutable_request_payload(self) -> dict[str, Any]:
        """Return the exact request content covered by idempotency hashing."""

        payload = self.model_dump(mode="json")
        return {field: payload[field] for field in _HASHED_REQUEST_FIELDS}

    @field_validator("input_json")
    @classmethod
    def input_must_not_contain_credentials(cls, value: dict[str, Any]) -> dict[str, Any]:
        return reject_secret_fields(value)

    @field_validator("executor_kind")
    @classmethod
    def executor_kind_must_be_known(cls, value: str) -> str:
        if value not in KNOWN_EXECUTOR_KINDS:
            raise ValueError(
                f"unknown executor_kind '{value}'; known kinds: "
                f"{', '.join(sorted(KNOWN_EXECUTOR_KINDS))}"
            )
        return value

    @field_validator("deadline_at")
    @classmethod
    def deadline_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value)
