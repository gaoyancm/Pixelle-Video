"""Phase 04-B migration tests: QC rules, profiles, seeds, and head."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0015_add_anime_series"
HEAD_REVISION = "0016_add_content_plans"
NEW_TABLES = {"qc_rules", "qc_profiles"}


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def test_phase4b_migration_has_one_head_and_follows_0006() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS_REVISION


def test_empty_upgrade_creates_two_tables(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        assert NEW_TABLES <= set(inspector.get_table_names())
        rule_columns = {column["name"] for column in inspector.get_columns("qc_rules")}
        assert {
            "id",
            "name",
            "category",
            "rule_type",
            "rule_config_json",
            "provider",
            "is_active",
            "priority",
            "created_at",
            "updated_at",
        } <= rule_columns
        profile_columns = {column["name"] for column in inspector.get_columns("qc_profiles")}
        assert {
            "id",
            "name",
            "description",
            "rules_json",
            "is_default",
            "created_at",
        } <= profile_columns
        rule_checks = {item["name"] for item in inspector.get_check_constraints("qc_rules")}
        assert "ck_qc_rules_category" in rule_checks
        assert "ck_qc_rules_rule_type" in rule_checks
    finally:
        engine.dispose()


def test_upgrade_seeds_twelve_rules_across_six_categories(tmp_path: Path) -> None:
    path = tmp_path / "seeded.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            rule_count = connection.execute(text("SELECT COUNT(*) FROM qc_rules")).scalar_one()
            categories = set(connection.execute(text("SELECT category FROM qc_rules")).scalars())
            profile_count = connection.execute(
                text("SELECT COUNT(*) FROM qc_profiles")
            ).scalar_one()
            default_count = connection.execute(
                text("SELECT COUNT(*) FROM qc_profiles WHERE is_default = 1")
            ).scalar_one()
            ffprobe_count = connection.execute(
                text("SELECT COUNT(*) FROM qc_rules WHERE provider = 'ffprobe'")
            ).scalar_one()
        assert rule_count >= 12
        assert categories == {"technical", "visual", "character", "narrative", "brand", "platform"}
        assert profile_count >= 2
        assert default_count == 1
        assert ffprobe_count >= 2
    finally:
        engine.dispose()


def test_profile_rules_reference_seeded_rules(tmp_path: Path) -> None:
    path = tmp_path / "profiles.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            default_rules = connection.execute(
                text("SELECT rules_json FROM qc_profiles WHERE id = 'profile-default'")
            ).scalar_one()
        assert "qc-resolution" in default_rules
        assert "qc-nsfw" in default_rules
    finally:
        engine.dispose()


def test_downgrade_removes_phase4b_tables(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, "0006_add_prompt_templates")
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
