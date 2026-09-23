"""switch submissions to a UUIDv7 primary key

Revision ID: 5b2c9e4d7a10
Revises: 877af0a23a74
Create Date: 2026-09-22 00:10:00.000000

The integer key is replaced in place: every existing row receives a fresh UUIDv7, generated
in ``created_at`` order so the new keys sort the same way the old ones did. The reverse
migration re-numbers the rows 1..n in the same order; identifiers are not preserved across
a round trip, so a downgrade is a re-identification rather than a restore.

MySQL commits DDL implicitly, so a failure half-way cannot be rolled back. Every step below
therefore checks the live schema first and only does what is still missing, which makes
``alembic upgrade head`` (and ``downgrade``) safe to re-run after an interruption.

Sized for this application: the swap rebuilds the table in one ``ALTER`` and backfills row
by row, which is fine for thousands of rows but would call for an expand/contract migration
with batched backfills on a large, busy table. The backfill needs a live connection, so the
migration refuses to run in Alembic's offline (``--sql``) mode.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "5b2c9e4d7a10"
down_revision: str | Sequence[str] | None = "877af0a23a74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "submissions"
INDEX = "ix_submissions_created_at"


def _require_online() -> Connection:
    if context.is_offline_mode():
        raise RuntimeError(
            "5b2c9e4d7a10 backfills rows and must run against a live database, not --sql"
        )
    return op.get_bind()


def _column_types(connection: Connection) -> dict[str, str]:
    columns = sa.inspect(connection).get_columns(TABLE)
    return {column["name"]: str(column["type"]).upper() for column in columns}


def _has_index(connection: Connection) -> bool:
    indexes = sa.inspect(connection).get_indexes(TABLE)
    return any(index["name"] == INDEX for index in indexes)


def upgrade() -> None:
    connection = _require_online()
    columns = _column_types(connection)

    if "BINARY" not in columns["id"]:  # still the integer key
        if "uuid" not in columns:
            op.add_column(TABLE, sa.Column("uuid", mysql.BINARY(16), nullable=True))
        # Rows keyed by an earlier, interrupted run keep their UUID.
        legacy_ids = list(
            connection.execute(
                sa.text(
                    "SELECT id FROM submissions WHERE uuid IS NULL ORDER BY created_at, id"
                )
            ).scalars()
        )
        for legacy_id in legacy_ids:
            connection.execute(
                sa.text("UPDATE submissions SET uuid = :uuid WHERE id = :id"),
                {"uuid": uuid.uuid7().bytes, "id": legacy_id},
            )
        # One statement, one table rebuild: MySQL lets the old key column go together with
        # the primary key, and the new column takes its name and its place.
        op.execute(
            "ALTER TABLE submissions"
            " DROP PRIMARY KEY,"
            " DROP COLUMN id,"
            " CHANGE uuid id BINARY(16) NOT NULL FIRST,"
            " ADD PRIMARY KEY (id)"
        )

    if not _has_index(connection):
        op.create_index(INDEX, TABLE, ["created_at"], unique=False)


def downgrade() -> None:
    connection = _require_online()

    if _has_index(connection):
        op.drop_index(INDEX, table_name=TABLE)

    columns = _column_types(connection)
    if "BINARY" in columns["id"]:  # still the UUID key
        if "legacy_id" not in columns:
            op.add_column(TABLE, sa.Column("legacy_id", sa.Integer(), nullable=True))
        # The numbering must be dense, so every row is (re)numbered on each attempt.
        current_ids = list(
            connection.execute(
                sa.text("SELECT id FROM submissions ORDER BY created_at, id")
            ).scalars()
        )
        for number, current_id in enumerate(current_ids, start=1):
            connection.execute(
                sa.text("UPDATE submissions SET legacy_id = :number WHERE id = :id"),
                {"number": number, "id": current_id},
            )
        op.execute(
            "ALTER TABLE submissions"
            " DROP PRIMARY KEY,"
            " DROP COLUMN id,"
            " CHANGE legacy_id id INT NOT NULL AUTO_INCREMENT FIRST,"
            " ADD PRIMARY KEY (id)"
        )
