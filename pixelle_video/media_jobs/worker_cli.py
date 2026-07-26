"""Command-line entry point for the independent persistent media worker."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path
from typing import Any

from pixelle_video.config.manager import ConfigManager
from pixelle_video.media_assets import AssetRepository, AssetService, LocalAssetStore
from pixelle_video.services.comfyui_adapter import ComfyUIAdapter

from .database import MediaJobsDatabase
from .executor import RecoverableComfyUIExecutor
from .repository import MediaJobRepository
from .worker import MediaJobWorker


def _managed_path(base_dir: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _build_executor(manager: ConfigManager, session_factory: Any) -> RecoverableComfyUIExecutor:
    """Build the exact production executor dependency graph without starting a Worker."""

    config = manager.config.media_jobs
    config_base = Path(config.config_base_dir or "").resolve()
    repository = MediaJobRepository(session_factory)
    asset_service = AssetService(
        AssetRepository(session_factory),
        LocalAssetStore(_managed_path(config_base, config.asset_store_root)),
        max_upload_size=config.asset_max_upload_size,
    )
    return RecoverableComfyUIExecutor(
        repository,
        ComfyUIAdapter(manager.config.comfyui.nodes),
        managed_asset_root=_managed_path(config_base, config.managed_asset_root),
        managed_output_root=_managed_path(config_base, config.managed_output_root),
        history_poll_interval_seconds=config.history_poll_interval_seconds,
        asset_service=asset_service,
    )


async def run_worker(args: argparse.Namespace) -> None:
    manager = ConfigManager(args.config)
    config = manager.config.media_jobs
    database = MediaJobsDatabase(config)
    session_factory = database.connect()
    executor = _build_executor(manager, session_factory)
    repository = executor.repository
    worker = MediaJobWorker(
        repository,
        executor,
        worker_id=args.worker_id,
        poll_interval_seconds=config.poll_interval_seconds,
        lease_seconds=config.lease_seconds,
        heartbeat_seconds=config.heartbeat_seconds,
        recovery_scan_interval_seconds=config.recovery_scan_interval_seconds,
    )
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
    args = parser.parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
