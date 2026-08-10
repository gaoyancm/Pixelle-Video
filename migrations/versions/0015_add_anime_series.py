"""Add the phase 07 anime series structure tables.

Revision ID: 0015_add_anime_series
Revises: 0014_add_anime_assets
Create Date: 2026-08-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_add_anime_series"
down_revision: Union[str, Sequence[str], None] = "0014_add_anime_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "anime_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("world_setting", sa.Text(), nullable=True),
        sa.Column("style_profile", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("anime_project_id", sa.String(length=36), nullable=True),
        sa.Column("season_no", sa.Integer(), nullable=False),
        sa.Column("episode_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("script_summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'producing', 'completed')",
            name="ck_episodes_status",
        ),
        sa.ForeignKeyConstraint(["anime_project_id"], ["anime_projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("anime_project_id", "season_no", "episode_no"),
    )
    op.create_table(
        "scenes_in_episode",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("episode_id", sa.String(length=36), nullable=True),
        sa.Column("scene_no", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.String(length=36), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "characters_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["location_id"], ["scenes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("episode_id", "scene_no"),
    )
    op.create_table(
        "shots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scene_id", sa.String(length=36), nullable=True),
        sa.Column("shot_no", sa.Integer(), nullable=False),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        sa.Column("camera_setup_json", sa.JSON(), nullable=True),
        sa.Column("visual_description", sa.Text(), nullable=False),
        sa.Column("character_states_json", sa.JSON(), nullable=True),
        sa.Column("image_prompt", sa.Text(), nullable=True),
        sa.Column("video_prompt", sa.Text(), nullable=True),
        sa.Column("parent_shot_id", sa.String(length=36), nullable=True),
        sa.Column("reference_shot_ids_json", sa.JSON(), nullable=True),
        sa.Column("generated_asset_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'succeeded', 'failed', 'rejected', 'done')",
            name="ck_shots_status",
        ),
        sa.ForeignKeyConstraint(["parent_shot_id"], ["shots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes_in_episode.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scene_id", "shot_no"),
    )


def downgrade() -> None:
    op.drop_table("shots")
    op.drop_table("scenes_in_episode")
    op.drop_table("episodes")
    op.drop_table("anime_projects")
