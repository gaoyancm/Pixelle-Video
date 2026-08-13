from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

import pixelle_video.anime.models as _anime_models  # noqa: F401
import pixelle_video.audit.models as _audit_models  # noqa: F401
import pixelle_video.budget.models as _budget_models  # noqa: F401
import pixelle_video.experiments.models as _experiment_models  # noqa: F401
import pixelle_video.knowledge.models as _knowledge_models  # noqa: F401
import pixelle_video.management.models as _management_models  # noqa: F401
import pixelle_video.media_assets.models as _media_assets_models  # noqa: F401
import pixelle_video.orchestration.models as _orchestration_models  # noqa: F401
import pixelle_video.products.models as _product_models  # noqa: F401
import pixelle_video.prompts.models as _prompt_models  # noqa: F401
import pixelle_video.qc.models as _qc_models  # noqa: F401
import pixelle_video.videos.models as _video_models  # noqa: F401
from pixelle_video.media_jobs.database import sqlite_url_for_path
from pixelle_video.media_jobs.models import Base

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0003_add_media_assets"
HEAD_REVISION = "0017_add_phase10_reference"
MANAGEMENT_TABLES = {
    "projects",
    "production_batches",
    "production_items",
    "production_item_assets",
    "production_item_attempts",
    "management_operations",
}


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def normalized_sql(value: object) -> str:
    return re.sub(r"[\s()'\"]+", "", str(value)).lower()


def test_phase3a_migration_has_one_head_and_follows_0003():
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == "0016_add_content_plans"


def test_empty_upgrade_creates_six_tables_priority_constraints_and_indexes(tmp_path):
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        assert MANAGEMENT_TABLES <= set(inspector.get_table_names())
        media_columns = {column["name"]: column for column in inspector.get_columns("media_jobs")}
        assert media_columns["priority"]["nullable"] is False
        assert normalized_sql(media_columns["priority"]["default"]) == "1"
        media_checks = {item["name"] for item in inspector.get_check_constraints("media_jobs")}
        assert "ck_media_jobs_priority" in media_checks
        media_indexes = {item["name"]: item for item in inspector.get_indexes("media_jobs")}
        assert media_indexes["ix_media_jobs_claim_priority"]["column_names"] == [
            "status",
            "priority",
            "next_attempt_at",
            "created_at",
            "job_id",
        ]
        assert {item["name"] for item in inspector.get_unique_constraints("production_items")} == {
            "uq_production_items_batch_position"
        }
        assert {
            item["name"] for item in inspector.get_unique_constraints("production_item_assets")
        } == {
            "uq_production_item_assets_item_asset_role",
            "uq_production_item_assets_item_role_position",
        }
        assert {
            item["name"] for item in inspector.get_unique_constraints("production_item_attempts")
        } == {"uq_production_item_attempts_media_job_id"}
        assert {
            item["name"] for item in inspector.get_unique_constraints("management_operations")
        } == {"uq_management_operations_scope_operation_key"}
    finally:
        engine.dispose()


def _insert_phase2_rows(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO media_jobs ("
                "job_id,workflow_type,workflow_key,executor_kind,provider,status,input_json,"
                "input_assets_json,submission_token,request_hash,retry_count,created_at,updated_at,"
                "output_metadata,version,remote_status,remote_termination_status"
                ") VALUES ("
                "'job-1','workflow','workflow.json','private_comfyui','private_comfyui','queued',"
                "'{}','[]','token','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'[]',1,'unknown','unknown')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO media_assets ("
                "id,kind,state,backend,object_key,original_filename,media_type,mime_type,size_bytes,"
                "sha256,source,created_at,updated_at"
                ") VALUES ("
                "'asset-1','input','available','local','objects/asset-1.png','input.png','image',"
                "'image/png',1,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
                "'upload',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO media_job_assets (job_id,asset_id,direction,role,position,created_at) "
                "VALUES ('job-1','asset-1','input','input_image',0,CURRENT_TIMESTAMP)"
            )
        )


def test_phase2_database_upgrade_preserves_job_asset_and_backfills_priority(tmp_path):
    path = tmp_path / "phase2.db"
    migration_config = config(path)
    command.upgrade(migration_config, PREVIOUS_REVISION)
    engine = create_engine(sync_url(path))
    _insert_phase2_rows(engine)
    engine.dispose()

    command.upgrade(migration_config, "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            assert (
                connection.execute(
                    text("SELECT priority FROM media_jobs WHERE job_id='job-1'")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT asset_id FROM media_job_assets WHERE job_id='job-1'")
                ).scalar_one()
                == "asset-1"
            )
            assert (
                connection.execute(
                    text("SELECT state FROM media_assets WHERE id='asset-1'")
                ).scalar_one()
                == "available"
            )
    finally:
        engine.dispose()


def test_repeated_upgrade_and_downgrade_reupgrade_restore_exact_phase2_schema(tmp_path):
    path = tmp_path / "roundtrip.db"
    migration_config = config(path)
    command.upgrade(migration_config, PREVIOUS_REVISION)
    engine = create_engine(sync_url(path))
    try:
        inspector = inspect(engine)
        before_tables = set(inspector.get_table_names())
        before_columns = {
            table: [column["name"] for column in inspector.get_columns(table)]
            for table in before_tables
            if table != "alembic_version"
        }
    finally:
        engine.dispose()

    command.upgrade(migration_config, "head")
    command.upgrade(migration_config, "head")
    command.downgrade(migration_config, PREVIOUS_REVISION)
    engine = create_engine(sync_url(path))
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == before_tables
        assert {
            table: [column["name"] for column in inspector.get_columns(table)]
            for table in before_tables
            if table != "alembic_version"
        } == before_columns
    finally:
        engine.dispose()
    command.upgrade(migration_config, "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == HEAD_REVISION
            )
    finally:
        engine.dispose()


def test_orm_metadata_has_zero_diff_against_migrated_database(tmp_path):
    path = tmp_path / "parity.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "target_metadata": Base.metadata,
                    "include_object": lambda obj, name, type_, reflected, compare_to: not (
                        type_ == "index" and name == "ix_media_jobs_claim_priority"
                    ),
                },
            )
            assert compare_metadata(context, Base.metadata) == []
            index = next(
                item
                for item in Base.metadata.tables["media_jobs"].indexes
                if item.name == "ix_media_jobs_claim_priority"
            )
            assert [
                getattr(
                    getattr(expression, "element", expression),
                    "name",
                    str(getattr(expression, "element", expression)),
                )
                for expression in index.expressions
            ] == ["status", "priority", "next_attempt_at", "created_at", "job_id"]
            ddl = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='ix_media_jobs_claim_priority'"
                )
            ).scalar_one()
            assert "priority DESC" in ddl
    finally:
        engine.dispose()


def test_all_management_foreign_keys_are_restrict_and_named_constraints_inspect(tmp_path):
    path = tmp_path / "foreign-keys.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        for table in (
            "production_batches",
            "production_items",
            "production_item_assets",
            "production_item_attempts",
        ):
            foreign_keys = inspector.get_foreign_keys(table)
            assert foreign_keys
            assert all(item["options"].get("ondelete") == "RESTRICT" for item in foreign_keys)
        assert {item["name"] for item in inspector.get_check_constraints("production_batches")} >= {
            "ck_production_batches_state",
            "ck_production_batches_default_priority",
        }
        assert {item["name"] for item in inspector.get_check_constraints("production_items")} >= {
            "ck_production_items_position",
            "ck_production_items_priority_override",
        }
    finally:
        engine.dispose()
