"""Add phase 10 reference-image fields.

- product_briefs.plan_id: trace a brief back to its source content plan,
  replacing the old practice of stashing plan metadata inside
  reference_images_json (which is now returned to real reference imagery).
- video_scripts.reference_image_id: optional first-frame / reference image
  asset id for the short-video I2V path.

Revision ID: 0017_add_phase10_reference
Revises: 0016_add_content_plans
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_add_phase10_reference"
down_revision: Union[str, Sequence[str], None] = "0016_add_content_plans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("product_briefs", sa.Column("plan_id", sa.String(length=36), nullable=True))
    op.add_column(
        "video_scripts", sa.Column("reference_image_id", sa.String(length=36), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("video_scripts", "reference_image_id")
    op.drop_column("product_briefs", "plan_id")
