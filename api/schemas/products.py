"""Strict public contracts for the phase 05 product & ad pipeline API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BriefStatus = Literal["draft", "submitted", "processing", "completed", "archived"]
PlatformName = Literal["etsy", "tiktok", "instagram", "meta", "youtube_shorts"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductBriefCreate(StrictModel):
    product_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    selling_points: list[str] = Field(default_factory=list, max_length=30)
    target_audience: str | None = Field(default=None, max_length=255)
    brand_profile_id: str | None = Field(default=None, max_length=64)
    platforms: list[PlatformName] = Field(default_factory=list, max_length=8)
    reference_images: list[str] = Field(default_factory=list, max_length=20)


class ProductBriefUpdate(StrictModel):
    product_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=20_000)
    category: str | None = Field(default=None, max_length=64)
    selling_points: list[str] | None = Field(default=None, max_length=30)
    target_audience: str | None = Field(default=None, max_length=255)
    platforms: list[PlatformName] | None = Field(default=None, max_length=8)
    status: BriefStatus | None = None


class ProductBriefResponse(StrictModel):
    id: str
    project_id: str | None
    product_name: str
    category: str | None
    description: str
    selling_points: list[str]
    target_audience: str | None
    brand_profile_id: str | None
    platforms: list[str]
    reference_images: list[str] | None
    status: str
    created_at: datetime
    updated_at: datetime


class ProductBriefList(StrictModel):
    items: list[ProductBriefResponse]
    has_more: bool


class IdeaOption(StrictModel):
    hook: str
    headline: str
    cta: str
    style: str


class GenerateIdeasResponse(StrictModel):
    brief_id: str
    ideas: list[IdeaOption]


class ConfirmResponse(StrictModel):
    brief_id: str
    status: str
    batch_id: str
    tasks: int
    jobs: dict[str, list[str]]


class ProgressResponse(StrictModel):
    brief_id: str
    status: str
    total_jobs: int
    completed_jobs: int
    running_jobs: int
    failed_jobs: int
    jobs_by_kind: dict[str, int]


class ResultAsset(StrictModel):
    job_id: str
    role: str
    platform: str | None
    asset_id: str | None
    file_path: str | None
    status: str


class ResultsResponse(StrictModel):
    brief_id: str
    groups: dict[str, list[ResultAsset]]


class AdaptRequest(StrictModel):
    platform: PlatformName
    output_dir: str | None = Field(default=None, max_length=1024)


class AdaptResponse(StrictModel):
    asset_id: str
    platform: str
    variant: dict[str, Any]


class PackageRequest(StrictModel):
    platforms: list[PlatformName] = Field(default_factory=list, max_length=8)


class PlatformPackage(StrictModel):
    directory: str
    files: list[str]
    metadata: dict[str, Any]


class PackageResponse(StrictModel):
    brief_id: str
    product_name: str
    delivery_root: str
    platforms: dict[str, PlatformPackage]


class PackageStatusResponse(StrictModel):
    brief_id: str
    packaged: bool
    delivery_root: str | None
    platforms: list[str]
