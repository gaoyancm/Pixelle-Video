"""Phase 04-C migration tests: experiment tables and head."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0007_add_qc_rules"
HEAD_REVISION = "0008_add_experiments"
NEW_TABLES = {"experiments", "experiment_groups", "experiment_jobs", "failure_samples"}


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase4c_migration_has_one_head_and_follows_0007() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS_REVISION


def test_empty_upgrade_creates_four_tables(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        assert NEW_TABLES <= set(inspector.get_table_names())
        experiment_columns = {column["name"] for column in inspector.get_columns("experiments")}
        assert {
            "id",
            "name",
            "description",
            "metric",
            "status",
            "created_at",
        } <= experiment_columns
        group_columns = {column["name"] for column in inspector.get_columns("experiment_groups")}
        assert {
            "id",
            "experiment_id",
            "group_name",
            "prompt_version_id",
            "model_name",
            "params_json",
        } <= group_columns
        job_columns = {column["name"] for column in inspector.get_columns("experiment_jobs")}
        assert {"job_id", "experiment_id", "group_id"} <= job_columns
        failure_columns = {column["name"] for column in inspector.get_columns("failure_samples")}
        assert {
            "id",
            "job_id",
            "experiment_id",
            "prompt_version_id",
            "reason",
            "qc_issues_json",
        } <= failure_columns
        experiment_checks = {
            item["name"] for item in inspector.get_check_constraints("experiments")
        }
        assert "ck_experiments_metric" in experiment_checks
        assert "ck_experiments_status" in experiment_checks
    finally:
        engine.dispose()


def test_downgrade_removes_phase4c_tables(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, PREVIOUS_REVISION)
    engine = create_engine(sync_url(path))
    try:
        assert NEW_TABLES.isdisjoint(set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()


def test_repeated_upgrade_head_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "repeat.db"
    value = config(path)
    command.upgrade(value, "head")
    command.upgrade(value, "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one()
        assert count == 1
    finally:
        engine.dispose()
