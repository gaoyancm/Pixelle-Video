"""Add media-job retry lineage.

Revision ID: 0002_add_media_job_retry_lineage
Revises: 0001_create_media_jobs
Create Date: 2026-07-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_media_job_retry_lineage"
down_revision: Union[str, Sequence[str], None] = "0001_create_media_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_jobs",
        sa.Column("retry_of_job_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_media_jobs_retry_of_job_id",
        "media_jobs",
        ["retry_of_job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_jobs_retry_of_job_id", table_name="media_jobs")
    op.drop_column("media_jobs", "retry_of_job_id")
