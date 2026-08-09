"""Phase 04-A migration tests: prompt template tables, seeds, and head."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0006_add_prompt_templates"
HEAD_REVISION = "0007_add_qc_rules"
NEW_TABLES = {
    "prompt_templates",
    "prompt_versions",
    "prompt_tags",
    "prompt_template_tags",
}


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase4a_migration_has_one_head_and_follows_0005() -> None:
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
        template_columns = {column["name"] for column in inspector.get_columns("prompt_templates")}
        assert {
            "id",
            "name",
            "category",
            "description",
            "template_text",
            "variables_json",
            "provider",
            "is_active",
            "current_score",
            "usage_count",
            "last_used_at",
            "archived_at",
            "created_at",
            "updated_at",
        } <= template_columns
        version_columns = {column["name"] for column in inspector.get_columns("prompt_versions")}
        assert {
            "id",
            "template_id",
            "version_no",
            "template_text",
            "variables_json",
        } <= version_columns
        tag_columns = {column["name"] for column in inspector.get_columns("prompt_tags")}
        assert {"id", "name"} <= tag_columns
        relation_columns = {
            column["name"] for column in inspector.get_columns("prompt_template_tags")
        }
        assert {"template_id", "tag_id"} <= relation_columns
    finally:
        engine.dispose()


def test_upgrade_seeds_seven_templates_and_twenty_one_tags(tmp_path: Path) -> None:
    path = tmp_path / "seeded.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            template_count = connection.execute(
                text("SELECT COUNT(*) FROM prompt_templates")
            ).scalar_one()
            tag_count = connection.execute(text("SELECT COUNT(*) FROM prompt_tags")).scalar_one()
            version_count = connection.execute(
                text("SELECT COUNT(*) FROM prompt_versions")
            ).scalar_one()
            relation_count = connection.execute(
                text("SELECT COUNT(*) FROM prompt_template_tags")
            ).scalar_one()
            names = set(connection.execute(text("SELECT name FROM prompt_templates")).scalars())
        assert template_count == 7
        assert tag_count >= 20
        assert version_count == 7
        assert relation_count >= 1
        assert {
            "视频标题生成",
            "内容旁白生成",
            "主题旁白生成",
            "视频提示词生成",
            "图片提示词生成",
            "风格转换提示词",
            "素材脚本生成",
        } <= names
    finally:
        engine.dispose()


def test_seeded_templates_have_version_one_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "versions.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            versions = connection.execute(
                text("SELECT template_id, version_no FROM prompt_versions ORDER BY template_id")
            ).fetchall()
        assert len(versions) == 7
        assert all(version_no == 1 for _template_id, version_no in versions)
    finally:
        engine.dispose()


def test_downgrade_removes_phase4a_tables(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, "0005_add_audit_and_budget")
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
