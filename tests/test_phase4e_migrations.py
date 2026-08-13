"""Phase 04-E migration tests: content plans table."""

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


def test_phase4e_migration_has_one_head_and_follows_0015() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS_REVISION


def test_upgrade_creates_content_plans_table(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        inspector = inspect(engine)
        assert "content_plans" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("content_plans")}
        assert {
            "id",
            "project_id",
            "request_text",
            "intent",
            "plan_json",
            "status",
            "cost_estimate",
            "checkpoint_json",
            "created_at",
        } <= columns
        checks = {item["name"] for item in inspector.get_check_constraints("content_plans")}
        assert "ck_content_plans_status" in checks
        assert "ck_content_plans_intent" in checks
    finally:
        engine.dispose()


def test_downgrade_removes_content_plans(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, "0015_add_anime_series")
    engine = create_engine(sync_url(path))
    try:
        assert "content_plans" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
