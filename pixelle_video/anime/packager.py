"""Episode delivery packaging for fully realized anime shots."""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pixelle_video.anime.repository import AnimeNotFoundError, AnimeRepository
from pixelle_video.media_assets.repository import AssetRepository


class AnimeDeliveryNotReadyError(RuntimeError):
    """The episode does not yet have a complete set of durable shot outputs."""


class AnimePackager:
    def __init__(
        self,
        repository: AnimeRepository,
        asset_repository: AssetRepository,
        *,
        asset_path_resolver: Callable[[Any], Path],
        exports_root: str | Path = "exports",
    ):
        self.repository = repository
        self.asset_repository = asset_repository
        self.asset_path_resolver = asset_path_resolver
        self.exports_root = Path(exports_root)

    async def package(self, episode_id: str) -> dict[str, Any]:
        episode = await self.repository.get_episode(episode_id)
        if episode is None:
            raise AnimeNotFoundError("episode not found")
        project = await self.repository.get_anime_project(episode.anime_project_id)
        if project is None:
            raise AnimeNotFoundError("anime project not found")

        scenes = await self.repository.list_scenes_in_episode(episode_id)
        shots = []
        for scene in scenes:
            shots.extend((scene, shot) for shot in await self.repository.list_shots(scene.id))
        if not shots:
            raise AnimeDeliveryNotReadyError("episode has no shots")
        if any(shot.status not in {"succeeded", "done"} for _scene, shot in shots):
            raise AnimeDeliveryNotReadyError("all episode shots must succeed before packaging")

        delivery_root = self.exports_root / project.project_id / episode_id / "anime_delivery"
        delivery_root.mkdir(parents=True, exist_ok=True)
        manifest_shots: list[dict[str, Any]] = []
        for scene, shot in shots:
            if not shot.generated_asset_id:
                raise AnimeDeliveryNotReadyError(f"shot '{shot.id}' has no output asset")
            asset = await self.asset_repository.get(shot.generated_asset_id)
            if asset is None or asset.state != "available":
                raise AnimeDeliveryNotReadyError(f"shot '{shot.id}' output asset is unavailable")
            source = self.asset_path_resolver(asset)
            if not source.is_file():
                raise AnimeDeliveryNotReadyError(f"shot '{shot.id}' output file is missing")
            suffix = source.suffix or Path(asset.original_filename).suffix or ".bin"
            filename = f"scene-{scene.scene_no:03d}-shot-{shot.shot_no:03d}{suffix.lower()}"
            destination = delivery_root / filename
            shutil.copy2(source, destination)
            manifest_shots.append(
                {
                    "scene_id": scene.id,
                    "scene_no": scene.scene_no,
                    "shot_id": shot.id,
                    "shot_no": shot.shot_no,
                    "asset_id": asset.id,
                    "file": filename,
                }
            )

        manifest = {
            "episode_id": episode_id,
            "anime_project_id": episode.anime_project_id,
            "project_id": project.project_id,
            "title": episode.title,
            "shots": manifest_shots,
        }
        (delivery_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        zip_path = delivery_root.parent / f"{episode_id}_anime_delivery.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in delivery_root.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(delivery_root))
        return {
            "episode_id": episode_id,
            "delivery_root": str(delivery_root),
            "zip_path": str(zip_path),
            "files": [entry["file"] for entry in manifest_shots],
        }

    async def download_path(self, episode_id: str) -> Path | None:
        episode = await self.repository.get_episode(episode_id)
        if episode is None:
            raise AnimeNotFoundError("episode not found")
        project = await self.repository.get_anime_project(episode.anime_project_id)
        if project is None:
            raise AnimeNotFoundError("anime project not found")
        path = self.exports_root / project.project_id / episode_id / f"{episode_id}_anime_delivery.zip"
        return path if path.is_file() else None
