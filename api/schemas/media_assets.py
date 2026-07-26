"""Public schemas for managed media assets."""

from datetime import datetime

from pydantic import BaseModel

from pixelle_video.media_assets import AssetKind, AssetState


class MediaAssetResponse(BaseModel):
    asset_id: str
    kind: AssetKind
    state: AssetState
    original_filename: str
    media_type: str
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    updated_at: datetime


class MediaAssetListResponse(BaseModel):
    items: list[MediaAssetResponse]
    limit: int
    offset: int
    has_more: bool


class AssetErrorDetail(BaseModel):
    code: str
    message: str


class AssetErrorResponse(BaseModel):
    error: AssetErrorDetail
