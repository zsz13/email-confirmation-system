import uuid
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, StringConstraints

from app.models.submission import EmailStatus

NAME_MAX_LENGTH = 100
EMAIL_MAX_LENGTH = 254  # RFC 5321 limit for a complete address

Name = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=NAME_MAX_LENGTH,
        # No control characters: the name is placed verbatim in the plain-text email body
        # and in the recipient display name handed to Mailjet.
        pattern=r"^[^\x00-\x1f\x7f-\x9f]+$",
    ),
]
Email = Annotated[EmailStr, Field(max_length=EMAIL_MAX_LENGTH)]


class SubmissionCreate(BaseModel):
    name: Name = Field(examples=["Daniyar"])
    email: Email = Field(examples=["daniyar-swe-demo-0923@mailinator.com"])


class SubmissionCreatedResponse(BaseModel):
    """The whole success body: a confirmation message and an opaque tracking id.

    The client already knows what it sent, and the stored record (its name, address,
    timestamps and provider details) is internal. The tracking id is the only handle given
    out: it reads the delivery state of this one submission and nothing else.
    """

    detail: str
    tracking_id: uuid.UUID = Field(
        description=(
            "Opaque identifier for GET /api/v1/submissions/{tracking_id}/status, which "
            "returns only the delivery state. A UUIDv7: hard to enumerate, but an "
            "identifier rather than a secret."
        )
    )


class DeliveryStatusResponse(BaseModel):
    """The public view of a submission: its delivery state and nothing else."""

    status: EmailStatus
