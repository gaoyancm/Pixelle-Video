"""Phase 05 A3: multi-platform format adaptation.

Hard-coded platform specs (no new tables) plus Pillow image cropping/scaling
and FFmpeg video conversion, both already project dependencies.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Hard-coded platform specifications from the phase 05 contract.
PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "etsy": {
        "image_size": (2700, 2025),
        "video_size": None,
        "max_seconds": None,
        "format": "jpeg",
    },
    "tiktok": {
        "image_size": None,
        "video_size": (1080, 1920),
        "max_seconds": 600,
        "format": "mp4",
    },
    "instagram": {
        "image_size": (1080, 1080),
        "video_size": (1080, 1920),
        "max_seconds": 60,
        "format": "mp4",
    },
    "meta": {
        "image_size": (1200, 628),
        "video_size": (1080, 1350),
        "max_seconds": 15,
        "format": "mp4",
    },
    "xiaohongshu": {
        "image_size": (1080, 1440),
        "video_size": (1080, 1920),
        "max_seconds": 900,
        "format": "mp4",
    },
    "youtube_shorts": {
        "image_size": None,
        "video_size": (1920, 1080),
        "max_seconds": 60,
        "format": "mp4",
    },
}


class PlatformNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetVariant:
    path: str
    platform: str
    width: int
    height: int
    format: str


class PlatformAdapter:
    """Produce platform-adapted variants of an image or video asset."""

    def __init__(self, *, ffmpeg: str = "ffmpeg", work_dir: str | None = None):
        self.ffmpeg = ffmpeg
        self.work_dir = Path(work_dir) if work_dir else Path(".") / ".adapter-work"

    @staticmethod
    def specs() -> dict[str, dict[str, Any]]:
        return dict(PLATFORM_SPECS)

    @staticmethod
    def spec_for(platform: str) -> dict[str, Any]:
        if platform not in PLATFORM_SPECS:
            raise PlatformNotFoundError(f"unsupported platform: {platform}")
        return dict(PLATFORM_SPECS[platform])

    def adapt(
        self,
        asset_path: str,
        platform: str,
        *,
        output_dir: str | None = None,
        is_video: bool | None = None,
    ) -> AssetVariant:
        """Adapt an asset to a platform spec, choosing image or video path."""
        spec = self.spec_for(platform)
        source = Path(asset_path)
        if not source.exists():
            raise FileNotFoundError(f"asset not found: {asset_path}")
        if is_video is None:
            is_video = source.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}
        target_dir = Path(output_dir) if output_dir else self.work_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        if is_video:
            size = spec.get("video_size")
            if size is None:
                raise PlatformNotFoundError(f"platform {platform} does not accept video")
            output = target_dir / f"{source.stem}_{platform}_{size[0]}x{size[1]}.mp4"
            self._adapt_video(source, output, size)
        else:
            size = spec.get("image_size")
            if size is None:
                raise PlatformNotFoundError(f"platform {platform} does not accept images")
            extension = "jpg" if spec.get("format") == "jpeg" else "png"
            output = target_dir / f"{source.stem}_{platform}_{size[0]}x{size[1]}.{extension}"
            self._adapt_image(source, output, size)

        return AssetVariant(
            path=str(output),
            platform=platform,
            width=size[0],
            height=size[1],
            format=output.suffix.lstrip("."),
        )

    # --- image ------------------------------------------------------------------

    def _adapt_image(self, source: Path, output: Path, size: tuple[int, int]) -> None:
        from PIL import Image

        with Image.open(source) as image:
            image = image.convert("RGB")
            target_ratio = size[0] / size[1]
            source_ratio = image.width / image.height
            if source_ratio > target_ratio:
                new_width = int(image.height * target_ratio)
                left = (image.width - new_width) // 2
                image = image.crop((left, 0, left + new_width, image.height))
            else:
                new_height = int(image.width / target_ratio)
                top = (image.height - new_height) // 2
                image = image.crop((0, top, image.width, top + new_height))
            image = image.resize(size, Image.LANCZOS)
            image.save(output, quality=90)

    # --- video ------------------------------------------------------------------

    def _adapt_video(self, source: Path, output: Path, size: tuple[int, int]) -> None:
        command = [
            self.ffmpeg,
            "-y",
            "-i",
            str(source),
            "-vf",
            f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
            f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(output),
        ]
        completed = asyncio.run(_run_command(command))
        if completed != 0:
            raise RuntimeError(f"ffmpeg conversion failed for {source.name}")


async def _run_command(command: list[str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    return process.returncode or 0


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None
