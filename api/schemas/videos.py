"""Strict public contracts for the phase 06 short-video pipeline API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ScriptStatus = Literal[
    "draft", "confirmed", "storyboarding", "assets", "composing", "completed", "archived"
]
VideoPlatform = Literal["tiktok", "reels", "youtube_shorts", "generic_landscape"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptGenerateRequest(StrictModel):
    topic: str = Field(min_length=1, max_length=512)
    language: str = Field(default="zh-CN", max_length=16)
    target_duration: int = Field(default=60, ge=5, le=600)
    platform: str = Field(default="tiktok", max_length=16)
    project_id: str | None = Field(default=None, max_length=64)


class ScriptUpdateRequest(StrictModel):
    script_json: dict[str, Any] | None = None
    status: ScriptStatus | None = None


class ScriptResponse(StrictModel):
    id: str
    project_id: str | None
    topic: str
    language: str
    target_duration: int
    platform: str
    script_json: dict[str, Any] | None
    prompt_version_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ConfirmResponse(StrictModel):
    script_id: str
    status: str


class StoryboardResponse(StrictModel):
    script_id: str
    frames: list[dict[str, Any]]


class GenerateAssetsResponse(StrictModel):
    script_id: str
    jobs: dict[str, str]


class AssetProgressResponse(StrictModel):
    script_id: str
    frames: list[dict[str, Any]]
    states: dict[str, int]


class ComposeResponse(StrictModel):
    script_id: str
    status: str
    video: str
    srt: str
    vtt: str
    audio: str | None
    bgm: str | None


class ComposeStatusResponse(StrictModel):
    script_id: str
    status: str
    completed: bool
    video: str | None


class PlatformVideoPackage(StrictModel):
    directory: str
    size: list[int]
    max_seconds: int | None
    languages: list[str]
    files: list[str]


class VideoPackageResponse(StrictModel):
    script_id: str
    topic: str
    delivery_root: str
    platforms: dict[str, PlatformVideoPackage]
