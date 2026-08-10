"""Phase 03-F migration tests: audit, budget, output schemas, cost columns."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0014_add_anime_assets"
HEAD_REVISION = "0015_add_anime_series"
NEW_TABLES = {"audit_events", "budget_config", "output_schemas"}


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase3f_migration_has_one_head_and_follows_0004() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS_REVISION


def test_empty_upgrade_creates_new_tables_and_columns(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        assert NEW_TABLES <= set(inspector.get_table_names())

        audit_columns = {column["name"] for column in inspector.get_columns("audit_events")}
        assert {
            "event_id",
            "event_type",
            "scope_type",
            "scope_id",
            "operator",
            "details_json",
            "cost_snapshot",
            "created_at",
        } <= audit_columns

        budget_columns = {column["name"] for column in inspector.get_columns("budget_config")}
        assert {"id", "per_task_limit", "per_batch_limit", "mode"} <= budget_columns
        budget_checks = {item["name"] for item in inspector.get_check_constraints("budget_config")}
        assert "ck_budget_config_mode" in budget_checks

        schema_columns = {column["name"] for column in inspector.get_columns("output_schemas")}
        assert {"schema_id", "workflow_type", "schema_json"} <= schema_columns
        schema_unique = {
            item["name"] for item in inspector.get_unique_constraints("output_schemas")
        }
        assert "uq_output_schemas_workflow_type" in schema_unique

        management_columns = {
            column["name"] for column in inspector.get_columns("management_operations")
        }
        assert "cost_snapshot" in management_columns

        media_columns = {column["name"] for column in inspector.get_columns("media_jobs")}
        assert {"estimated_cost", "actual_cost", "budget_warning"} <= media_columns
    finally:
        engine.dispose()


def test_upgrade_seeds_budget_config_and_output_schemas(tmp_path: Path) -> None:
    path = tmp_path / "seeded.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            budget = connection.execute(text("SELECT id, mode FROM budget_config")).fetchall()
            assert [row[0] for row in budget] == ["global"]
            assert {row[1] for row in budget} == {"observe"}
            workflows = set(
                connection.execute(text("SELECT workflow_type FROM output_schemas")).scalars()
            )
            assert workflows == {"a800_wan22_t2v_33f", "gpu_4090_wan21_i2v_33f"}
    finally:
        engine.dispose()


def test_downgrade_removes_phase3f_objects(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    command.upgrade(config(path), "head")
    command.downgrade(config(path), "0004_add_management_domain")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        table_names = set(inspector.get_table_names())
        assert NEW_TABLES.isdisjoint(table_names)
        management_columns = {
            column["name"] for column in inspector.get_columns("management_operations")
        }
        assert "cost_snapshot" not in management_columns
        media_columns = {column["name"] for column in inspector.get_columns("media_jobs")}
        assert "estimated_cost" not in media_columns
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
