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

from api.services.experiments import ExperimentApplicationService
from api.services.knowledge import KnowledgeApplicationService
from api.services.management import ManagementApplicationService
from api.services.media_jobs import MediaJobApplicationService
from api.services.products import ProductApplicationService
from api.services.prompts import PromptApplicationService
from api.services.qc import QCApplicationService
from api.services.videos import VideoApplicationService
from pixelle_video.audit import AuditRepository
from pixelle_video.budget import BudgetRepository, BudgetService
from pixelle_video.config.manager import ConfigManager
from pixelle_video.experiments.repository import ExperimentRepository
from pixelle_video.knowledge.repository import KnowledgeRepository
from pixelle_video.management import ManagementRepository
from pixelle_video.media_assets import AssetRepository, AssetService, LocalAssetStore
from pixelle_video.media_jobs import MediaJobRepository, MediaJobsDatabase
from pixelle_video.products.ad_engine import AdProductionEngine
from pixelle_video.products.delivery import DeliveryPackager
from pixelle_video.products.platform_adapter import PlatformAdapter
from pixelle_video.products.repository import ProductBriefRepository
from pixelle_video.prompts.compiler import compile
from pixelle_video.prompts.repository import PromptRepository
from pixelle_video.qc.executor import QCExecutor
from pixelle_video.qc.repository import QCRepository
from pixelle_video.service import PixelleVideoCore
from pixelle_video.services.comfyui_adapter import select_comfyui_node
from pixelle_video.services.tts_service import TTSService
from pixelle_video.videos.compose import Composer
from pixelle_video.videos.packager import VideoPackager
from pixelle_video.videos.repository import VideoScriptRepository
from pixelle_video.videos.script_engine import ScriptEngine
from pixelle_video.videos.storyboard import StoryboardEngine

# Global Pixelle-Video instance
_pixelle_video_instance: PixelleVideoCore = None
_media_jobs_database: MediaJobsDatabase | None = None
_media_jobs_service: MediaJobApplicationService | None = None
_media_assets_service: AssetService | None = None
_management_service: ManagementApplicationService | None = None
_audit_repository: AuditRepository | None = None
_budget_service: BudgetService | None = None
_prompt_service: PromptApplicationService | None = None
_qc_service: QCApplicationService | None = None
_experiment_service: ExperimentApplicationService | None = None
_knowledge_service: KnowledgeApplicationService | None = None
_product_service: ProductApplicationService | None = None
_video_service: VideoApplicationService | None = None


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
        manager = ConfigManager()
        config = manager.config.media_jobs
        _media_jobs_database = MediaJobsDatabase(config)
        repository = MediaJobRepository(
            _media_jobs_database.connect(), audit=await get_audit_service()
        )
        assets = await get_media_asset_service()
        _media_jobs_service = MediaJobApplicationService(
            repository,
            config,
            assets,
            node_selector=lambda workflow_type: select_comfyui_node(
                manager.config.comfyui.nodes,
                workflow_type,
            ).id,
            budget=await get_budget_service(),
            audit=await get_audit_service(),
        )
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


async def get_management_service() -> ManagementApplicationService:
    """Lazily build the management service without migration or external calls."""

    global _management_service, _media_jobs_database
    if _management_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        sessions = _media_jobs_database.connect()
        assets = await get_media_asset_service()
        _management_service = ManagementApplicationService(
            ManagementRepository(sessions, audit=await get_audit_service()),
            config,
            assets,
            node_selector=lambda workflow_type: select_comfyui_node(
                manager.config.comfyui.nodes, workflow_type
            ).id,
            configured_nodes=manager.config.comfyui.nodes,
            budget=await get_budget_service(),
        )
    return _management_service


async def get_audit_service() -> AuditRepository:
    """Lazily build the audit repository on the shared media-jobs database."""

    global _audit_repository, _media_jobs_database
    if _audit_repository is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        _audit_repository = AuditRepository(_media_jobs_database.connect())
    return _audit_repository


async def get_budget_service() -> BudgetService:
    """Lazily build the budget service on the shared media-jobs database."""

    global _budget_service, _media_jobs_database
    if _budget_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        sessions = _media_jobs_database.connect()
        _budget_service = BudgetService(
            BudgetRepository(sessions),
            sessions,
            job_repository=MediaJobRepository(sessions),
        )
    return _budget_service


async def get_prompt_service() -> PromptApplicationService:
    """Lazily build the prompt template service on the shared media-jobs database."""

    global _prompt_service, _media_jobs_database
    if _prompt_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        _prompt_service = PromptApplicationService(PromptRepository(_media_jobs_database.connect()))
    return _prompt_service


async def get_qc_service() -> QCApplicationService:
    """Lazily build the QC pipeline service on the shared media-jobs database."""

    global _qc_service, _media_jobs_database
    if _qc_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        sessions = _media_jobs_database.connect()
        job_repository = MediaJobRepository(sessions)

        async def resolve_output_path(job_id: str) -> str | None:
            assets = await get_media_asset_service()
            rows = await assets.repository.output_assets_for_job(job_id)
            if not rows:
                return None
            _relation, asset = rows[0]
            if asset.state != "available" or not asset.object_key:
                return None
            try:
                return str(assets.store.local_path(asset.object_key))
            except Exception:
                return None

        executor = QCExecutor(
            QCRepository(sessions),
            job_lookup=job_repository.get_job,
            output_path_resolver=resolve_output_path,
        )
        _qc_service = QCApplicationService(
            QCRepository(sessions),
            executor,
            audit=await get_audit_service(),
        )
    return _qc_service


async def shutdown_media_jobs() -> None:
    global _management_service, _media_assets_service, _media_jobs_database
    global _media_jobs_service, _audit_repository, _budget_service, _prompt_service, _qc_service
    global _experiment_service, _knowledge_service, _product_service, _video_service
    if _media_jobs_database is not None:
        await _media_jobs_database.dispose()
    _media_jobs_database = None
    _media_jobs_service = None
    _media_assets_service = None
    _management_service = None
    _audit_repository = None
    _budget_service = None
    _prompt_service = None
    _qc_service = None
    _experiment_service = None
    _knowledge_service = None
    _product_service = None
    _video_service = None


async def get_experiment_service() -> ExperimentApplicationService:
    """Lazily build the experiment service on the shared media-jobs database."""

    global _experiment_service, _media_jobs_database
    if _experiment_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        sessions = _media_jobs_database.connect()
        job_repository = MediaJobRepository(sessions)

        async def resolve_output_size(job_id: str) -> int | None:
            assets = await get_media_asset_service()
            rows = await assets.repository.output_assets_for_job(job_id)
            if not rows:
                return None
            _relation, asset = rows[0]
            return asset.size_bytes

        _experiment_service = ExperimentApplicationService(
            ExperimentRepository(sessions),
            job_lookup=job_repository.get_job,
            audit=await get_audit_service(),
            size_lookup=resolve_output_size,
        )
    return _experiment_service


async def get_knowledge_service() -> KnowledgeApplicationService:
    """Lazily build the knowledge base service on the shared media-jobs database."""

    global _knowledge_service, _media_jobs_database
    if _knowledge_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        _knowledge_service = KnowledgeApplicationService(
            KnowledgeRepository(_media_jobs_database.connect()),
            audit=await get_audit_service(),
        )
    return _knowledge_service


async def get_product_service() -> ProductApplicationService:
    """Lazily build the phase 05 product pipeline on shared repositories."""

    global _product_service, _media_jobs_database
    if _product_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        sessions = _media_jobs_database.connect()
        job_repository = MediaJobRepository(sessions)
        management_repository = ManagementRepository(sessions)
        brief_repository = ProductBriefRepository(sessions)
        asset_repository = AssetRepository(sessions)
        ad_engine = AdProductionEngine(
            brief_repository,
            management_repository,
            job_repository,
        )
        qc_executor = QCExecutor(
            QCRepository(sessions),
            job_lookup=job_repository.get_job,
        )
        packager = DeliveryPackager(
            brief_repository,
            exports_root="exports",
            qc_runner=qc_executor.run_qc,
        )
        _product_service = ProductApplicationService(
            brief_repository,
            ad_engine=ad_engine,
            asset_repository=asset_repository,
            job_repository=job_repository,
            platform_adapter=PlatformAdapter(),
            delivery_packager=packager,
            prompt_compiler=compile,
            qc_runner=qc_executor.run_qc,
        )
    return _product_service


async def get_video_service() -> VideoApplicationService:
    """Lazily build the phase 06 short-video pipeline on shared repositories."""

    global _video_service, _media_jobs_database
    if _video_service is None:
        manager = ConfigManager()
        config = manager.config.media_jobs
        if _media_jobs_database is None:
            _media_jobs_database = MediaJobsDatabase(config)
        sessions = _media_jobs_database.connect()
        script_repository = VideoScriptRepository(sessions)
        job_repository = MediaJobRepository(sessions)
        asset_repository = AssetRepository(sessions)
        # TTS runner: the repository's workflow-based TTS service (injected,
        # not invoked during wiring).
        tts_runner = None
        try:
            tts_service = TTSService(manager.config.to_dict())
            tts_runner = tts_service.__call__
        except Exception:
            logger.warning("TTS service unavailable for video pipeline")
        bgm_matcher = _video_bgm_matcher

        async def resolve_frame_asset(job_id: str) -> str | None:
            rows = await asset_repository.output_assets_for_job(job_id)
            if not rows:
                return None
            asset = rows[0][1]
            return getattr(asset, "file_path", None) or getattr(asset, "path", None)

        composer = Composer(
            script_repository,
            tts_runner=tts_runner,
            bgm_matcher=bgm_matcher,
            asset_resolver=resolve_frame_asset,
        )
        _video_service = VideoApplicationService(
            script_repository,
            script_engine=ScriptEngine(script_repository, prompt_compiler=compile),
            storyboard_engine=StoryboardEngine(script_repository, job_repository),
            composer=composer,
            packager=VideoPackager(script_repository, exports_root="exports"),
            job_repository=job_repository,
            prompt_compiler=compile,
        )
    return _video_service


def _video_bgm_matcher(emotion: str) -> str | None:
    """Deterministic emotion -> BGM mapping (reserved injection point)."""
    mapping = {"激昂": "bgm_energetic", "舒缓": "bgm_calm", "科技感": "bgm_tech"}
    return mapping.get(emotion)


# Type alias for dependency injection
PixelleVideoDep = Annotated[PixelleVideoCore, Depends(get_pixelle_video)]
MediaJobServiceDep = Annotated[MediaJobApplicationService, Depends(get_media_job_service)]
MediaAssetServiceDep = Annotated[AssetService, Depends(get_media_asset_service)]
ManagementServiceDep = Annotated[ManagementApplicationService, Depends(get_management_service)]
AuditServiceDep = Annotated[AuditRepository, Depends(get_audit_service)]
BudgetServiceDep = Annotated[BudgetService, Depends(get_budget_service)]
PromptServiceDep = Annotated[PromptApplicationService, Depends(get_prompt_service)]
QCServiceDep = Annotated[QCApplicationService, Depends(get_qc_service)]
ExperimentServiceDep = Annotated[ExperimentApplicationService, Depends(get_experiment_service)]
KnowledgeServiceDep = Annotated[KnowledgeApplicationService, Depends(get_knowledge_service)]
ProductServiceDep = Annotated[ProductApplicationService, Depends(get_product_service)]
VideoServiceDep = Annotated[VideoApplicationService, Depends(get_video_service)]
