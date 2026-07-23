import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect, text

from pixelle_video.media_jobs.database import sqlite_url_for_path
from pixelle_video.media_jobs.models import MediaJob

PROJECT_ROOT = Path(__file__).parents[1]


def alembic_config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url_for_path(database_path))
    return config


def sync_sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve().as_posix()}"


def test_alembic_upgrades_empty_database_to_head(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    command.upgrade(alembic_config(database_path), "head")

    engine = create_engine(sync_sqlite_url(database_path))
    inspector = inspect(engine)
    try:
        assert "media_jobs" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("media_jobs")}
        assert {
            "job_id",
            "status",
            "input_json",
            "input_assets_json",
            "remote_status",
            "remote_termination_status",
            "version",
        } <= columns
        index_names = {index["name"] for index in inspector.get_indexes("media_jobs")}
        assert {
            "ix_media_jobs_status",
            "ix_media_jobs_status_next_attempt",
            "ix_media_jobs_next_attempt_at",
            "ix_media_jobs_lease_expires_at",
        } <= index_names
        version = engine.execute if False else None
        del version
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == "0001_create_media_jobs"
    finally:
        engine.dispose()


def test_repeated_alembic_upgrade_head_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "repeat-migration.db"
    config = alembic_config(database_path)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine(sync_sqlite_url(database_path))
    try:
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM alembic_version")
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


def test_migration_databases_are_created_only_in_pytest_temp_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "location-check.db"
    command.upgrade(alembic_config(database_path), "head")

    assert database_path.exists()
    assert database_path.parent == tmp_path
    assert not (PROJECT_ROOT / "data" / "media_jobs.db").exists()


def _normalized_sql(value: object) -> str:
    return re.sub(r"[\s()'\"]+", "", str(value)).lower()


def test_orm_metadata_matches_migrated_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "metadata-parity.db"
    command.upgrade(alembic_config(database_path), "head")
    engine = create_engine(sync_sqlite_url(database_path))
    inspector = inspect(engine)
    table = MediaJob.__table__
    try:
        reflected_columns = {
            column["name"]: column for column in inspector.get_columns("media_jobs")
        }
        assert set(reflected_columns) == {column.name for column in table.columns}
        for model_column in table.columns:
            reflected = reflected_columns[model_column.name]
            assert reflected["nullable"] is model_column.nullable
            assert reflected["type"]._type_affinity is model_column.type._type_affinity
            model_default = (
                None
                if model_column.server_default is None
                else _normalized_sql(model_column.server_default.arg)
            )
            reflected_default = (
                None
                if reflected["default"] is None
                else _normalized_sql(reflected["default"])
            )
            assert reflected_default == model_default, model_column.name

        model_unique = {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        reflected_unique = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("media_jobs")
        }
        assert reflected_unique == model_unique

        model_checks = {
            constraint.name: _normalized_sql(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        reflected_checks = {
            constraint["name"]: _normalized_sql(constraint["sqltext"])
            for constraint in inspector.get_check_constraints("media_jobs")
        }
        assert reflected_checks == model_checks

        model_indexes = {
            index.name: (tuple(column.name for column in index.columns), index.unique)
            for index in table.indexes
        }
        reflected_indexes = {
            index["name"]: (tuple(index["column_names"]), index["unique"])
            for index in inspector.get_indexes("media_jobs")
        }
        assert reflected_indexes == model_indexes
    finally:
        engine.dispose()
