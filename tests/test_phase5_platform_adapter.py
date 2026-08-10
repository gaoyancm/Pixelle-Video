"""Phase 05 A3 platform adapter tests (Pillow + FFmpeg)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from pixelle_video.products.platform_adapter import (
    PLATFORM_SPECS,
    PlatformAdapter,
    PlatformNotFoundError,
)


@pytest.fixture
def adapter(tmp_path: Path) -> PlatformAdapter:
    return PlatformAdapter(work_dir=str(tmp_path / "adapt-work"))


def _make_image(path: Path, size: tuple[int, int] = (2000, 1200)) -> Path:
    image = Image.new("RGB", size, color=(180, 60, 60))
    image.save(path)
    return path


def test_specs_cover_five_platforms() -> None:
    assert set(PLATFORM_SPECS) == {"etsy", "tiktok", "instagram", "meta", "youtube_shorts"}
    assert PLATFORM_SPECS["etsy"]["image_size"] == (2700, 2025)
    assert PLATFORM_SPECS["tiktok"]["video_size"] == (1080, 1920)
    assert PLATFORM_SPECS["instagram"]["image_size"] == (1080, 1080)
    assert PLATFORM_SPECS["meta"]["image_size"] == (1200, 628)
    assert PLATFORM_SPECS["youtube_shorts"]["video_size"] == (1920, 1080)


def test_spec_for_unknown_platform_raises() -> None:
    with pytest.raises(PlatformNotFoundError):
        PlatformAdapter.spec_for("wechat")


def test_adapt_image_crops_and_scales(tmp_path: Path, adapter: PlatformAdapter) -> None:
    source = _make_image(tmp_path / "hero.jpg", (2000, 1200))
    variant = adapter.adapt(str(source), "etsy")
    assert variant.platform == "etsy"
    assert (variant.width, variant.height) == (2700, 2025)
    assert Path(variant.path).exists()
    with Image.open(variant.path) as image:
        assert image.size == (2700, 2025)


def test_adapt_image_square_for_instagram(tmp_path: Path, adapter: PlatformAdapter) -> None:
    source = _make_image(tmp_path / "square.jpg", (1600, 800))
    variant = adapter.adapt(str(source), "instagram")
    assert (variant.width, variant.height) == (1080, 1080)
    with Image.open(variant.path) as image:
        assert image.size == (1080, 1080)


def test_adapt_video_converts_to_mp4(tmp_path: Path, adapter: PlatformAdapter) -> None:
    if not _ffmpeg():
        pytest.skip("ffmpeg not available")
    source = _make_video(tmp_path / "clip.webm")
    variant = adapter.adapt(str(source), "tiktok", is_video=True)
    assert variant.platform == "tiktok"
    assert (variant.width, variant.height) == (1080, 1920)
    assert Path(variant.path).suffix == ".mp4"
    assert Path(variant.path).exists()


def test_adapt_video_meta_portrait(tmp_path: Path, adapter: PlatformAdapter) -> None:
    if not _ffmpeg():
        pytest.skip("ffmpeg not available")
    source = _make_video(tmp_path / "meta.webm")
    variant = adapter.adapt(str(source), "meta", is_video=True)
    assert (variant.width, variant.height) == (1080, 1350)


def test_adapt_image_unsupported_platform_raises(tmp_path: Path, adapter: PlatformAdapter) -> None:
    source = _make_image(tmp_path / "img.jpg")
    with pytest.raises(PlatformNotFoundError):
        adapter.adapt(str(source), "tiktok", is_video=False)


def test_adapt_missing_source_raises(adapter: PlatformAdapter) -> None:
    with pytest.raises(FileNotFoundError):
        adapter.adapt(str(Path("no-such-file.jpg")), "etsy")


def _ffmpeg() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None


def _make_video(path: Path) -> Path:
    import subprocess

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=640x360:rate=15",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path
