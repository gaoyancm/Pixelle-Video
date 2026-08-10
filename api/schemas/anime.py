"""Strict public contracts for the phase 07 anime pipeline API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ShotStatus = Literal["pending", "queued", "succeeded", "failed", "rejected", "done"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CharacterCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = Field(default=None, max_length=64)
    role_type: str | None = Field(default=None, max_length=32)
    identity_anchors: dict[str, Any] = Field(default_factory=dict)
    static_features: dict[str, Any] = Field(default_factory=dict)
    dynamic_features: dict[str, Any] | None = None
    reference_images: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    voice_config: dict[str, Any] | None = None
    auto_fill_anchors: bool = Field(default=False)


class CharacterUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=20_000)
    identity_anchors: dict[str, Any] | None = None
    static_features: dict[str, Any] | None = None
    dynamic_features: dict[str, Any] | None = None
    reference_images: list[dict[str, Any]] | None = Field(default=None, max_length=20)


class CharacterResponse(StrictModel):
    id: str
    project_id: str | None
    name: str
    role_type: str | None
    description: str
    identity_anchors: dict[str, Any]
    static_features: dict[str, Any]
    dynamic_features: dict[str, Any] | None
    reference_images: list[dict[str, Any]]
    voice_config: dict[str, Any] | None
    created_at: datetime


class SceneAssetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = Field(default=None, max_length=64)
    environment: dict[str, Any] | None = None
    reference_images: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class PropCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    project_id: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=20_000)
    reference_images: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class AnimeProjectCreate(StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    world_setting: str | None = Field(default=None, max_length=20_000)
    style_profile: str | None = Field(default=None, max_length=64)


class EpisodeCreate(StrictModel):
    anime_project_id: str = Field(min_length=1, max_length=64)
    season_no: int = Field(default=1, ge=1)
    episode_no: int = Field(default=1, ge=1)
    title: str | None = Field(default=None, max_length=255)
    script_summary: str | None = Field(default=None, max_length=20_000)


class SceneInEpisodeCreate(StrictModel):
    episode_id: str = Field(min_length=1, max_length=64)
    scene_no: int = Field(ge=1)
    description: str | None = Field(default=None, max_length=20_000)
    location_id: str | None = Field(default=None, max_length=64)
    characters: list[dict[str, Any]] = Field(default_factory=list, max_length=30)


class ShotCreate(StrictModel):
    scene_id: str = Field(min_length=1, max_length=64)
    shot_no: int = Field(ge=1)
    duration_sec: int = Field(ge=1, le=600)
    visual_description: str = Field(min_length=1, max_length=20_000)
    camera_setup: dict[str, Any] | None = None
    character_states: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    parent_shot_id: str | None = Field(default=None, max_length=64)
    reference_shot_ids: list[str] = Field(default_factory=list, max_length=20)


class ShotPlanResponse(StrictModel):
    shots: list[dict[str, Any]]


class ShotGenerateResponse(StrictModel):
    shot_id: str
    status: str
    job_id: str
    parent_shot_id: str | None


class SceneGenerateResponse(StrictModel):
    scene_id: str
    shots: list[ShotGenerateResponse]


class ShotProgressResponse(StrictModel):
    scene_id: str
    total: int
    states: dict[str, int]
    shots: list[dict[str, Any]]


class ConsistencyReportResponse(StrictModel):
    character_id: str
    character_name: str
    static_features: dict[str, Any]
    dynamic_features: dict[str, Any] | None
    reports: list[dict[str, Any]]
