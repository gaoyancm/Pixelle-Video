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
            "retry_of_job_id",
        } <= columns
        index_names = {index["name"] for index in inspector.get_indexes("media_jobs")}
        assert {
            "ix_media_jobs_status",
            "ix_media_jobs_status_next_attempt",
            "ix_media_jobs_next_attempt_at",
            "ix_media_jobs_lease_expires_at",
            "ix_media_jobs_retry_of_job_id",
        } <= index_names
        version = engine.execute if False else None
        del version
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "0005_add_audit_and_budget"
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
            count = connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one()
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
                None if reflected["default"] is None else _normalized_sql(reflected["default"])
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

        model_indexes = {}
        for index in table.indexes:
            names = []
            for expression in index.expressions:
                element = getattr(expression, "element", expression)
                names.append(getattr(element, "name", str(element)))
            model_indexes[index.name] = (tuple(names), index.unique)
        reflected_indexes = {
            index["name"]: (tuple(index["column_names"]), index["unique"])
            for index in inspector.get_indexes("media_jobs")
        }
        assert reflected_indexes == model_indexes
    finally:
        engine.dispose()


def test_retry_lineage_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "round-trip.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0001_create_media_jobs")
    engine = create_engine(sync_sqlite_url(database_path))
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("media_jobs")}
        assert "retry_of_job_id" not in columns
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    engine = create_engine(sync_sqlite_url(database_path))
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("media_jobs")}
        assert "retry_of_job_id" in columns
    finally:
        engine.dispose()


def test_media_assets_migration_from_0002_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "assets-round-trip.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0002_add_media_job_retry_lineage")
    engine = create_engine(sync_sqlite_url(database_path))
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version", "media_jobs"}
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(sync_sqlite_url(database_path))
    try:
        inspector = inspect(engine)
        assert {"media_assets", "media_job_assets"} <= set(inspector.get_table_names())
        assert {
            "id",
            "kind",
            "state",
            "backend",
            "object_key",
            "sha256",
            "disabled_at",
            "deleted_at",
        } <= {column["name"] for column in inspector.get_columns("media_assets")}
        assert len(inspector.get_foreign_keys("media_job_assets")) == 2
    finally:
        engine.dispose()

    command.downgrade(config, "0002_add_media_job_retry_lineage")
    engine = create_engine(sync_sqlite_url(database_path))
    try:
        inspector = inspect(engine)
        assert "media_assets" not in inspector.get_table_names()
        assert "media_job_assets" not in inspector.get_table_names()
        assert "retry_of_job_id" in {
            column["name"] for column in inspector.get_columns("media_jobs")
        }
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(sync_sqlite_url(database_path))
    try:
        assert {"media_assets", "media_job_assets"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
