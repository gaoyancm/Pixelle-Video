"""Command-line entry point for the independent persistent media worker."""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import signal
from pathlib import Path
from typing import Any

from pixelle_video.config.loader import load_config_dict
from pixelle_video.config.schema import PixelleVideoConfig
from pixelle_video.media_assets import AssetRepository, AssetService, LocalAssetStore
from pixelle_video.orchestration.llm.deepseek_caller import deepseek_llm_caller
from pixelle_video.services.comfyui_adapter import ComfyUIAdapter

from .database import MediaJobsDatabase
from .dispatcher import DispatchingJobProcessor, build_default_processors
from .executor import RecoverableComfyUIExecutor
from .repository import MediaJobRepository
from .worker import MediaJobWorker


def _managed_path(base_dir: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _load_config(config_path: str) -> PixelleVideoConfig:
    """Load a config file directly, bypassing the module-level ConfigManager
    singleton (which is bound to the default config.yaml on first import and
    would silently ignore a caller-supplied path)."""

    config = PixelleVideoConfig(**load_config_dict(config_path))
    config.media_jobs.set_config_base_dir(str(Path(config_path).resolve().parent))
    return config


def _build_executor(config: PixelleVideoConfig, session_factory: Any) -> RecoverableComfyUIExecutor:
    """Build the exact production executor dependency graph without starting a Worker."""

    media_jobs = config.media_jobs
    config_base = Path(media_jobs.config_base_dir or "").resolve()
    repository = MediaJobRepository(session_factory)
    asset_service = AssetService(
        AssetRepository(session_factory),
        LocalAssetStore(_managed_path(config_base, media_jobs.asset_store_root)),
        max_upload_size=media_jobs.asset_max_upload_size,
    )
    return RecoverableComfyUIExecutor(
        repository,
        ComfyUIAdapter(config.comfyui.nodes),
        managed_asset_root=_managed_path(config_base, media_jobs.managed_asset_root),
        managed_output_root=_managed_path(config_base, media_jobs.managed_output_root),
        history_poll_interval_seconds=media_jobs.history_poll_interval_seconds,
        asset_service=asset_service,
    )


def _build_llm_caller(config: PixelleVideoConfig):
    """Return the production caption text generator, or a failing stub offline.

    The stub raises instead of silently fabricating copy, so an unconfigured
    deployment fails its caption jobs rather than shipping template text.
    """

    api_key = (os.environ.get("DEEPSEEK_API_KEY", "") or "").strip() or (
        config.llm.api_key or ""
    ).strip()
    base_url = (config.llm.base_url or "").strip()
    model = (config.llm.model or "").strip()
    if api_key and base_url and model:
        return functools.partial(
            deepseek_llm_caller,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    async def unconfigured_caller(_prompt: str) -> str:
        raise RuntimeError("LLM is not configured; cannot generate captions")

    return unconfigured_caller


async def run_worker(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    media_jobs = config.media_jobs
    database = MediaJobsDatabase(media_jobs)
    session_factory = database.connect()
    executor = _build_executor(config, session_factory)
    repository = executor.repository
    dispatcher = DispatchingJobProcessor(
        repository,
        build_default_processors(
            repository,
            comfyui_executor=executor,
            llm_caller=_build_llm_caller(config),
            managed_output_root=_managed_path(
                Path(media_jobs.config_base_dir or "").resolve(),
                media_jobs.managed_output_root,
            ),
        ),
    )
    worker = MediaJobWorker(
        repository,
        dispatcher,
        worker_id=args.worker_id,
        poll_interval_seconds=media_jobs.poll_interval_seconds,
        lease_seconds=media_jobs.lease_seconds,
        heartbeat_seconds=media_jobs.heartbeat_seconds,
        recovery_scan_interval_seconds=media_jobs.recovery_scan_interval_seconds,
    )
    if args.check:
        # Readiness probe: prove DB reachability + loaded processor registry.
        await database.verify_connection()
        print(
            f"worker ready: worker_id={worker.worker_id} "
            f"processors={sorted(dispatcher.processor_kinds())}",
            flush=True,
        )
        await database.dispose()
        return

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, worker.request_stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        if args.once:
            await worker.run_once()
        else:
            await worker.run_forever()
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the persistent media job worker")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--worker-id")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one bounded claim/processing scan and exit",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Initialize DB + processor registry, print ready, and exit",
    )
    args = parser.parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
