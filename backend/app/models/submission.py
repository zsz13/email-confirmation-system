import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UUIDBinary


class EmailStatus(StrEnum):
    """Where the confirmation email is, in Mailjet's terms.

    Declared in lifecycle order, which is also how MySQL sorts the ENUM column. Mailjet
    accepting ``POST /v3.1/send`` only makes a message ``accepted``; ``delivered`` means the
    recipient's mail server took it, which still says nothing about Inbox placement. See
    ``app.services.delivery_status`` for how Mailjet's statuses and events map onto these.
    """

    PENDING = "pending"  # stored, not yet handed to Mailjet
    ACCEPTED = "accepted"  # Mailjet's Send API accepted it for sending
    QUEUED = "queued"  # Mailjet has it queued
    RETRYING = "retrying"  # temporary failure (Mailjet "deferred"); Mailjet retries
    DELIVERED = "delivered"  # the recipient's mail server accepted it (Mailjet "sent")
    SOFT_BOUNCED = "soft_bounced"
    HARD_BOUNCED = "hard_bounced"
    BLOCKED = "blocked"  # Mailjet refused to send it
    FAILED = "failed"  # the Send API call itself failed


class Submission(Base):
    __tablename__ = "submissions"

    # UUIDv7 (RFC 9562): a millisecond timestamp in the high bits, so keys are generated in
    # the application without a round trip, are globally unique, and still insert in order.
    # The public API hands it out only as the opaque tracking id for the status endpoint;
    # the admin panel shows the canonical string form.
    id: Mapped[uuid.UUID] = mapped_column(
        UUIDBinary, primary_key=True, default=uuid.uuid7
    )
    name: Mapped[str] = mapped_column(String(100))
    # Deliberately not unique: the same inbox may be used for repeated demonstrations.
    email: Mapped[str] = mapped_column(String(254))
    email_status: Mapped[EmailStatus] = mapped_column(
        Enum(
            EmailStatus,
            name="email_status",
            values_callable=lambda members: [m.value for m in members],
        ),
        default=EmailStatus.PENDING,
    )
    # The id Mailjet gave the message, needed to look its status up (and, with a webhook,
    # to find the row an event is about). Internal: never returned by the public API.
    mailjet_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # Indexed: the admin panel lists submissions newest first.
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC), index=True
    )
