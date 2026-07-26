# Copyright (C) 2025 AIDC-AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
FastAPI Dependencies

Provides dependency injection for PixelleVideoCore and other services.
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends
from loguru import logger

from api.services.media_jobs import MediaJobApplicationService
from pixelle_video.config.manager import ConfigManager
from pixelle_video.media_assets import AssetRepository, AssetService, LocalAssetStore
from pixelle_video.media_jobs import MediaJobRepository, MediaJobsDatabase
from pixelle_video.service import PixelleVideoCore

# Global Pixelle-Video instance
_pixelle_video_instance: PixelleVideoCore = None
_media_jobs_database: MediaJobsDatabase | None = None
_media_jobs_service: MediaJobApplicationService | None = None
_media_assets_service: AssetService | None = None


async def get_pixelle_video() -> PixelleVideoCore:
    """
    Get Pixelle-Video core instance (dependency injection)
    
    Returns:
        PixelleVideoCore instance
    """
    global _pixelle_video_instance
    
    if _pixelle_video_instance is None:
        _pixelle_video_instance = PixelleVideoCore()
        await _pixelle_video_instance.initialize()
        logger.info("✅ Pixelle-Video initialized for API")
    
    return _pixelle_video_instance


async def shutdown_pixelle_video():
    """Shutdown Pixelle-Video instance and cleanup resources"""
    global _pixelle_video_instance
    if _pixelle_video_instance:
        logger.info("Shutting down Pixelle-Video...")
        await _pixelle_video_instance.cleanup()
        _pixelle_video_instance = None
    
    from pixelle_video.services.frame_html import HTMLFrameGenerator
    await HTMLFrameGenerator.close_browser()


async def get_media_job_service() -> MediaJobApplicationService:
    """Lazily connect to the configured schema without migrating or creating tables."""

    global _media_jobs_database, _media_jobs_service
    if _media_jobs_service is None:
        config = ConfigManager().config.media_jobs
        _media_jobs_database = MediaJobsDatabase(config)
        repository = MediaJobRepository(_media_jobs_database.connect())
        assets = await get_media_asset_service()
        _media_jobs_service = MediaJobApplicationService(repository, config, assets)
    return _media_jobs_service


async def get_media_asset_service() -> AssetService:
    """Lazily build the local asset service on the media-jobs database."""

    global _media_jobs_database, _media_assets_service
    if _media_assets_service is None:
        config = ConfigManager().config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        session_factory = _media_jobs_database.connect()
        base_dir = Path(config.config_base_dir or Path(__file__).resolve().parents[1])
        root = Path(config.asset_store_root)
        if not root.is_absolute():
            root = base_dir / root
        _media_assets_service = AssetService(
            AssetRepository(session_factory),
            LocalAssetStore(root),
            max_upload_size=config.asset_max_upload_size,
        )
    return _media_assets_service


async def shutdown_media_jobs() -> None:
    global _media_assets_service, _media_jobs_database, _media_jobs_service
    if _media_jobs_database is not None:
        await _media_jobs_database.dispose()
    _media_jobs_database = None
    _media_jobs_service = None
    _media_assets_service = None


# Type alias for dependency injection
PixelleVideoDep = Annotated[PixelleVideoCore, Depends(get_pixelle_video)]
MediaJobServiceDep = Annotated[MediaJobApplicationService, Depends(get_media_job_service)]
MediaAssetServiceDep = Annotated[AssetService, Depends(get_media_asset_service)]
