"""Phase 05 migration tests: product briefs table and head."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0014_add_anime_assets"
HEAD_REVISION = "0015_add_anime_series"


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase5_migration_has_one_head_and_follows_0010() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS_REVISION


def test_upgrade_creates_product_briefs_table(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        inspector = inspect(engine)
        assert "product_briefs" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("product_briefs")}
        assert {
            "id",
            "project_id",
            "product_name",
            "category",
            "description",
            "selling_points_json",
            "target_audience",
            "brand_profile_id",
            "platforms_json",
            "reference_images_json",
            "status",
            "created_at",
            "updated_at",
        } <= columns
        checks = {item["name"] for item in inspector.get_check_constraints("product_briefs")}
        assert "ck_product_briefs_status" in checks
    finally:
        engine.dispose()


def test_downgrade_removes_product_briefs(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, "0010_seed_knowledge")
    engine = create_engine(sync_url(path))
    try:
        assert "product_briefs" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
