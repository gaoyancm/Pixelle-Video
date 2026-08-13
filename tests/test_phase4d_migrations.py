"""Phase 04-D migration and seed tests: knowledge tables, head, seeds."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from pixelle_video.media_jobs.database import sqlite_url_for_path

PROJECT_ROOT = Path(__file__).parents[1]
PREVIOUS_REVISION = "0008_add_experiments"
HEAD_REVISION = "0017_add_phase10_reference"
NEW_TABLES = {
    "knowledge_entries",
    "knowledge_tags",
    "knowledge_entry_tags",
    "knowledge_links",
}


def config(path: Path) -> Config:
    value = Config(str(PROJECT_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", sqlite_url_for_path(path))
    return value


def sync_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def engine_factory(tmp_path: Path):
    def build(name: str):
        path = tmp_path / name
        command.upgrade(config(path), "head")
        return create_engine(sync_url(path))

    return build


def test_phase4d_migration_has_one_head_and_follows_0008() -> None:
    script = ScriptDirectory.from_config(config(Path("unused.db")))
    assert script.get_heads() == [HEAD_REVISION]
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None and revision.down_revision == "0016_add_content_plans"
    build = script.get_revision("0009_add_knowledge")
    assert build is not None and build.down_revision == PREVIOUS_REVISION


def test_empty_upgrade_creates_four_tables(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "head")
    engine = create_engine(sync_url(path))
    inspector = inspect(engine)
    try:
        assert NEW_TABLES <= set(inspector.get_table_names())
        entry_columns = {column["name"] for column in inspector.get_columns("knowledge_entries")}
        assert {
            "id",
            "title",
            "content",
            "category",
            "evidence_class",
            "status",
            "source_url",
            "source_doc",
            "verified_at",
            "created_at",
            "updated_at",
        } <= entry_columns
        entry_checks = {
            item["name"] for item in inspector.get_check_constraints("knowledge_entries")
        }
        assert "ck_knowledge_entries_category" in entry_checks
        assert "ck_knowledge_entries_evidence_class" in entry_checks
        assert "ck_knowledge_entries_status" in entry_checks
        link_columns = {column["name"] for column in inspector.get_columns("knowledge_links")}
        assert {"entry_id", "target_type", "target_id", "link_note"} <= link_columns
    finally:
        engine.dispose()


def test_upgrade_seeds_entries_covering_all_categories(tmp_path: Path) -> None:
    engine = engine_factory(tmp_path)("seeded.db")
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM knowledge_entries")).scalar_one()
            categories = set(
                connection.execute(text("SELECT category FROM knowledge_entries")).scalars()
            )
            evidence = set(
                connection.execute(text("SELECT evidence_class FROM knowledge_entries")).scalars()
            )
            documented_fact = connection.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE evidence_class = 'documented_fact'"
                )
            ).scalar_one()
            empirical = connection.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE evidence_class = 'empirical_observation'"
                )
            ).scalar_one()
            tag_count = connection.execute(text("SELECT COUNT(*) FROM knowledge_tags")).scalar_one()
            link_count = connection.execute(
                text("SELECT COUNT(*) FROM knowledge_entry_tags")
            ).scalar_one()
        assert count >= 20
        assert categories == {
            "策划",
            "平台规则",
            "镜头叙事",
            "角色场景",
            "模型工作流",
            "品牌产品",
            "后处理",
            "故障诊断",
        }
        assert evidence == {"documented_fact", "empirical_observation", "production_heuristic"}
        assert documented_fact >= 5
        assert empirical >= 3
        assert tag_count >= 10
        assert link_count >= count
    finally:
        engine.dispose()


def test_seed_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    value = config(path)
    command.upgrade(value, "head")
    command.upgrade(value, "head")
    engine = create_engine(sync_url(path))
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM knowledge_entries")).scalar_one()
            assert count >= 20
    finally:
        engine.dispose()


def test_downgrade_removes_phase4d_objects(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    value = config(path)
    command.upgrade(value, "head")
    command.downgrade(value, PREVIOUS_REVISION)
    engine = create_engine(sync_url(path))
    try:
        assert NEW_TABLES.isdisjoint(set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()
