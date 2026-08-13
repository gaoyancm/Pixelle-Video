"""Phase 07 migration tests: anime assets and series tables."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0013_add_video_scripts"
HEAD_REVISION = "0017_add_phase10_reference"


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase7_migration_has_one_head_and_follows_0013() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == "0016_add_content_plans"
    build = script.get_revision("0014_add_anime_assets")
    assert build is not None and build.down_revision == PREVIOUS_REVISION


def test_upgrade_creates_anime_tables(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "characters",
            "scenes",
            "props",
            "anime_projects",
            "episodes",
            "scenes_in_episode",
            "shots",
        } <= tables
        character_columns = {c["name"] for c in inspector.get_columns("characters")}
        assert {
            "identity_anchors_json",
            "static_features_json",
            "dynamic_features_json",
            "reference_images_json",
            "voice_config_json",
        } <= character_columns
        shot_columns = {c["name"] for c in inspector.get_columns("shots")}
        assert {"parent_shot_id", "camera_setup_json", "generated_asset_id"} <= shot_columns
        # parent_shot_id is a self-referencing FK
        fks = inspector.get_foreign_keys("shots")
        assert any(fk["referred_table"] == "shots" for fk in fks)
    finally:
        engine.dispose()


def test_downgrade_removes_anime_tables(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, "0013_add_video_scripts")
    engine = create_engine(sync_url(path))
    try:
        tables = set(inspect(engine).get_table_names())
        assert not {"characters", "shots", "episodes"} & tables
    finally:
        engine.dispose()
