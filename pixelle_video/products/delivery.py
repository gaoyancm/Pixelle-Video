"""Phase 05 A4: delivery package assembly and export."""

from __future__ import annotations

import inspect
import json
import shutil
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from pixelle_video.products.platform_adapter import PLATFORM_SPECS
from pixelle_video.products.repository import ProductBriefRepository


class DeliveryPackager:
    """Assemble a per-platform delivery package with QC and metadata."""

    def __init__(
        self,
        brief_repository: ProductBriefRepository,
        *,
        resolved_assets: dict[str, list[tuple[Any, Any]]] | None = None,
        qc_job_id: str | None = None,
        qc_runner: Callable[[str], Any] | None = None,
        exports_root: str | None = None,
    ):
        self.brief_repository = brief_repository
        self.resolved_assets = resolved_assets or {}
        self.qc_job_id = qc_job_id
        self.qc_runner = qc_runner
        self.exports_root = Path(exports_root) if exports_root else Path(".") / "exports"

    async def package(
        self,
        brief_id: str,
        platforms: Sequence[str],
        *,
        include_qc: bool = True,
        captions: dict[str, dict[str, Any]] | None = None,
        resolved_assets: dict[str, list[tuple[Any, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Package the brief's outputs into per-platform delivery folders.

        ``captions`` maps a platform to ``{"text": str, "job_id": str, "status": str}``
        from the successful caption job. When absent or unsuccessful, captions are
        written from the template and explicitly marked as a degraded fallback.
        """
        brief = await self.brief_repository.get_brief(brief_id)
        if brief is None:
            from pixelle_video.products.repository import ProductBriefNotFoundError

            raise ProductBriefNotFoundError("product brief not found")

        project_key = brief.project_id or "default-project"
        delivery_root = self.exports_root / project_key / brief.id / "delivery"
        delivery_root.mkdir(parents=True, exist_ok=True)

        platforms = [p for p in platforms if p in PLATFORM_SPECS]
        packaged: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            platform_dir = delivery_root / platform
            platform_dir.mkdir(parents=True, exist_ok=True)
            files = self._collect_outputs(platform_dir, resolved_assets or self.resolved_assets)
            self._write_captions(
                platform_dir, brief, platform, (captions or {}).get(platform)
            )
            metadata = await self._build_metadata(brief, platform, files, include_qc)
            self._write_metadata(platform_dir, metadata)
            packaged[platform] = {
                "directory": str(platform_dir),
                "files": [str(path.name) for path in files],
                "metadata": metadata,
            }

        return {
            "brief_id": brief_id,
            "product_name": brief.product_name,
            "delivery_root": str(delivery_root),
            "platforms": packaged,
        }

    def _collect_outputs(
        self,
        platform_dir: Path,
        resolved_assets: dict[str, list[tuple[Any, Any]]],
    ) -> list[Path]:
        """Copy resolved production outputs into this platform's package."""
        files: list[Path] = []
        for job_id, rows in resolved_assets.items():
            for _relation, asset in rows:
                path = getattr(asset, "file_path", None) or getattr(asset, "path", None)
                if path is None:
                    path = getattr(asset, "local_path", None)
                if not path or not Path(path).is_file():
                    continue
                source = Path(path)
                destination = platform_dir / f"{job_id[:8]}-{source.name}"
                shutil.copy2(source, destination)
                files.append(destination)
        return files

    @staticmethod
    def _captions_for(brief, platform: str) -> dict[str, list[str]]:
        product = brief.product_name
        points = brief.selling_points_json or []
        return {
            "captions": [
                f"{product}｜{'、'.join(points) if points else '精选'}｜适用 {platform} 平台",
                f"【{product}】{'，'.join(points[:3]) if points else '品质之选'}，限时关注了解更多",
            ],
            "hashtags": [f"#{product}", "#广告", f"#{platform}", "#好物推荐"],
        }

    async def _build_metadata(
        self, brief, platform: str, files: Sequence[Path], include_qc: bool
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "brief_id": brief.id,
            "product_name": brief.product_name,
            "platform": platform,
            "generated_at": date.today().isoformat(),
            "files": [str(path.name) for path in files],
            "prompt": {
                "role": f"ad_{platform}",
                "product": brief.product_name,
                "selling_points": brief.selling_points_json,
            },
            "model": "ad-production-pipeline",
            "parameters": {"platform": platform},
            "qc": None,
        }
        if include_qc and self.qc_runner is not None and self.qc_job_id:
            try:
                result = self.qc_runner(self.qc_job_id)
                if inspect.isawaitable(result):
                    result = await result
                if hasattr(result, "to_payload"):
                    result = result.to_payload()
                metadata["qc"] = result
            except Exception:
                metadata["qc"] = None
        return metadata

    @staticmethod
    def _write_captions(
        platform_dir: Path,
        brief,
        platform: str,
        caption: dict[str, Any] | None,
    ) -> None:
        if caption and (caption.get("text") or "").strip():
            source = "caption_job"
            text = caption["text"].strip()
        else:
            source = "fallback_template"
            text = "\n\n".join(DeliveryPackager._captions_for(brief, platform)["captions"])

        hashtags = DeliveryPackager._captions_for(brief, platform)["hashtags"]
        (platform_dir / "captions.txt").write_text(text, encoding="utf-8")
        (platform_dir / "hashtags.txt").write_text(" ".join(hashtags), encoding="utf-8")
        (platform_dir / "captions_source.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "job_id": caption.get("job_id") if caption else None,
                    "status": caption.get("status") if caption else None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_metadata(platform_dir: Path, metadata: dict[str, Any]) -> None:
        (platform_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def zip_delivery(self, brief_id: str, project_id: str | None = None) -> str | None:
        """Zip an existing delivery directory; returns the zip path or None."""
        project_key = project_id or "default-project"
        delivery_root = self.exports_root / project_key / brief_id / "delivery"
        if not delivery_root.exists():
            return None
        zip_path = delivery_root.parent / f"{brief_id}_delivery.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in delivery_root.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(delivery_root))
        return str(zip_path)
