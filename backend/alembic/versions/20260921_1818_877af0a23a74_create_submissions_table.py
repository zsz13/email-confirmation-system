"""create submissions table

Revision ID: 877af0a23a74
Revises:
Create Date: 2026-09-21 18:18:35.347133

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "877af0a23a74"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column(
            "email_status",
            sa.Enum("pending", "sent", "failed", name="email_status"),
            nullable=False,
        ),
        # Naive UTC with microseconds; app.db.types.UTCDateTime re-attaches the zone on load.
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("submissions")
