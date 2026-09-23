"""track Mailjet delivery state and keep the Mailjet MessageID

Revision ID: c3d8e1f4a6b2
Revises: 5b2c9e4d7a10
Create Date: 2026-09-22 12:00:00.000000

``sent`` used to mean "Mailjet's Send API accepted the message", which is not delivery. It is
renamed ``accepted``, and the states Mailjet reports afterwards are added. The MessageID the
Send API returns is stored so the delivery state can be looked up (or matched to a webhook
event) later.

The ENUM is widened first, the rows are rewritten, and only then is it narrowed, so no row
ever holds a value its column does not allow. MySQL commits DDL implicitly, so every step is
safe to re-run after an interruption: MODIFY and UPDATE are idempotent, and the column and
index are only added (or dropped) when they are still missing (or present).

Deploy order: the old code writes ``sent``, which the new column rejects, and the new code
writes ``accepted``, which the old one rejects, so schema and code change together. Compose
guarantees that: ``migrate`` (the same image as the backend) must finish before the backend
starts. Sized for this application: inserting ENUM members before ``failed`` and dropping
``sent`` makes MySQL copy the table, and the rename is one UPDATE. On a large, busy table
this would be an expand/contract change: append the new members, dual-write, backfill in
batches, and drop ``sent`` in a later release.

The downgrade maps every post-acceptance state back to ``sent`` and the bounced and blocked
ones to ``failed``, the closest the old model can express; that loses the distinction the
upgrade introduced, which is inherent in going back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "c3d8e1f4a6b2"
down_revision: str | Sequence[str] | None = "5b2c9e4d7a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "submissions"
COLUMN = "mailjet_message_id"
INDEX = "ix_submissions_mailjet_message_id"

OLD_STATUSES = ("pending", "sent", "failed")
NEW_STATUSES = (
    "pending",
    "accepted",
    "queued",
    "retrying",
    "delivered",
    "soft_bounced",
    "hard_bounced",
    "blocked",
    "failed",
)


def _set_statuses(statuses: Sequence[str]) -> None:
    values = ", ".join(f"'{status}'" for status in statuses)
    op.execute(f"ALTER TABLE {TABLE} MODIFY email_status ENUM({values}) NOT NULL")


def _inspector() -> sa.Inspector:
    # The existence checks need a live connection; there is nothing to inspect under --sql.
    if context.is_offline_mode():
        raise RuntimeError(
            "c3d8e1f4a6b2 inspects the live schema and cannot run with --sql"
        )
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    return {column["name"] for column in _inspector().get_columns(TABLE)}


def _has_index() -> bool:
    indexes = _inspector().get_indexes(TABLE)
    return any(index["name"] == INDEX for index in indexes)


def upgrade() -> None:
    # Both sets at once: 'pending' and 'failed' keep their meaning, 'sent' is on its way out.
    _set_statuses((*NEW_STATUSES, "sent"))
    op.execute(
        "UPDATE submissions SET email_status = 'accepted' WHERE email_status = 'sent'"
    )
    _set_statuses(NEW_STATUSES)

    if COLUMN not in _columns():
        op.add_column(TABLE, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    if not _has_index():
        op.create_index(INDEX, TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    if _has_index():
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns():
        op.drop_column(TABLE, COLUMN)

    _set_statuses((*NEW_STATUSES, "sent"))
    op.execute(
        "UPDATE submissions SET email_status = 'sent'"
        " WHERE email_status IN ('accepted', 'queued', 'retrying', 'delivered')"
    )
    op.execute(
        "UPDATE submissions SET email_status = 'failed'"
        " WHERE email_status IN ('soft_bounced', 'hard_bounced', 'blocked')"
    )
    _set_statuses(OLD_STATUSES)
