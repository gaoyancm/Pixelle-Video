"""Phase 06 migration tests: video scripts table and head."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0016_add_content_plans"
HEAD_REVISION = "0017_add_phase10_reference"


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase6_migration_has_one_head_and_follows_0012() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS_REVISION


def test_upgrade_creates_video_scripts_table(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        inspector = inspect(engine)
        assert "video_scripts" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("video_scripts")}
        assert {
            "id",
            "project_id",
            "topic",
            "language",
            "target_duration",
            "platform",
            "script_json",
            "prompt_version_id",
            "status",
            "created_at",
            "updated_at",
        } <= columns
        checks = {item["name"] for item in inspector.get_check_constraints("video_scripts")}
        assert "ck_video_scripts_status" in checks
        assert "ck_video_scripts_target_duration" in checks
    finally:
        engine.dispose()


def test_downgrade_removes_video_scripts(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, "0012_seed_ad_prompt")
    engine = create_engine(sync_url(path))
    try:
        assert "video_scripts" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
