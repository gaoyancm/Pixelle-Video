"""Phase 06 S4: multi-platform packaging for short videos."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Sequence

from pixelle_video.videos.repository import VideoScriptRepository

# Platform packaging specs (duration bounds + aspect + language variants).
VIDEO_PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "size": (1080, 1920),
        "max_seconds": 60,
        "label": "竖屏+Hook前3秒",
    },
    "reels": {
        "size": (1080, 1920),
        "max_seconds": 90,
        "label": "竖屏+封面",
    },
    "youtube_shorts": {
        "size": (1920, 1080),
        "max_seconds": 60,
        "label": "横屏+缩略图",
    },
    "generic_landscape": {
        "size": (1920, 1080),
        "max_seconds": None,
        "label": "通用横版",
    },
}


class VideoPackager:
    """Generate per-platform video versions plus titles, descriptions, covers."""

    def __init__(
        self,
        script_repository: VideoScriptRepository,
        *,
        exports_root: str | None = None,
    ):
        self.script_repository = script_repository
        self.exports_root = Path(exports_root) if exports_root else Path(".") / "exports"

    async def package(
        self,
        script_id: str,
        platforms: Sequence[str],
    ) -> dict[str, Any]:
        script = await self._require(script_id)
        languages = self._languages_for(script.language)
        project_key = script.project_id or "default-project"
        delivery_root = self.exports_root / project_key / script_id / "video_delivery"
        delivery_root.mkdir(parents=True, exist_ok=True)

        platforms = [p for p in platforms if p in VIDEO_PLATFORM_SPECS]
        packaged: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            spec = VIDEO_PLATFORM_SPECS[platform]
            platform_dir = delivery_root / platform
            platform_dir.mkdir(parents=True, exist_ok=True)
            self._write_titles(platform_dir, script, platform, languages)
            self._write_description(platform_dir, script, platform)
            self._write_cover_placeholder(platform_dir, script, platform, spec)
            packaged[platform] = {
                "directory": str(platform_dir),
                "size": list(spec["size"]),
                "max_seconds": spec["max_seconds"],
                "languages": languages,
                "files": sorted(path.name for path in platform_dir.iterdir()),
            }
        return {
            "script_id": script_id,
            "topic": script.topic,
            "delivery_root": str(delivery_root),
            "platforms": packaged,
        }

    def zip_package(self, script_id: str, project_id: str | None = None) -> str | None:
        project_key = project_id or "default-project"
        delivery_root = self.exports_root / project_key / script_id / "video_delivery"
        if not delivery_root.exists():
            return None
        zip_path = delivery_root.parent / f"{script_id}_video_package.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in delivery_root.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(delivery_root))
        return str(zip_path)

    # --- helpers --------------------------------------------------------------------

    def _languages_for(self, primary: str) -> list[str]:
        languages = [primary]
        if "zh" in primary and "en" not in languages:
            languages.append("en-US")
        elif "en" in primary and "zh-CN" not in languages:
            languages.append("zh-CN")
        return languages

    def _titles_for(self, script, platform: str, languages: Sequence[str]) -> dict[str, str]:
        titles: dict[str, str] = {}
        for language in languages:
            if language.startswith("zh"):
                titles[language] = f"{script.topic}｜90秒看懂｜#{platform} 精选"
            else:
                titles[language] = f"{script.topic} in 60 seconds | #{platform} picks"
        return titles

    def _descriptions_for(self, script, platform: str, languages: Sequence[str]) -> dict[str, str]:
        descriptions: dict[str, str] = {}
        for language in languages:
            if language.startswith("zh"):
                descriptions[language] = (
                    f"{script.topic}——快速了解，一键关注。#短视频 #{platform} #涨知识"
                )
            else:
                descriptions[language] = f"{script.topic} — quick overview. #shorts #{platform}"
        return descriptions

    def _write_titles(
        self, platform_dir: Path, script, platform: str, languages: Sequence[str]
    ) -> None:
        (platform_dir / "titles.json").write_text(
            json.dumps(self._titles_for(script, platform, languages), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_description(self, platform_dir: Path, script, platform: str) -> None:
        languages = self._languages_for(script.language)
        (platform_dir / "descriptions.json").write_text(
            json.dumps(
                self._descriptions_for(script, platform, languages),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_cover_placeholder(
        self, platform_dir: Path, script, platform: str, spec: dict[str, Any]
    ) -> None:
        size = spec["size"]
        cover = platform_dir / f"cover_{size[0]}x{size[1]}.txt"
        cover.write_text(
            f"封面占位（合成自首帧+标题）：{script.topic} @ {size[0]}x{size[1]}",
            encoding="utf-8",
        )

    async def _require(self, script_id: str):
        script = await self.script_repository.get_script(script_id)
        if script is None:
            from pixelle_video.videos.repository import VideoScriptNotFoundError

            raise VideoScriptNotFoundError("video script not found")
        return script
