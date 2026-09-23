import logging
import time
import uuid
from http import HTTPStatus

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import (
    ClientIpDep,
    EmailServiceDep,
    RateLimiterDep,
    SessionDep,
    StatusRateLimiterDep,
)
from app.core.logging import mask_email
from app.models.submission import EmailStatus, Submission
from app.schemas.error import ErrorResponse
from app.schemas.submission import (
    DeliveryStatusResponse,
    SubmissionCreate,
    SubmissionCreatedResponse,
)
from app.services.email import EmailDeliveryError

logger = logging.getLogger(__name__)

router = APIRouter()

# Acceptance, not delivery: the final state is only known later (see the reconciler).
EMAIL_ACCEPTED_DETAIL = "Confirmation email accepted for delivery."
EMAIL_FAILED_DETAIL = (
    "Your submission was saved, but the confirmation email could not be sent. "
    "Please try again later."
)
# One answer for a malformed id and for a well-formed one that matches nothing, so a
# response never tells a caller which of the two it sent.
TRACKING_ID_NOT_FOUND_DETAIL = "No submission matches this tracking ID."
STATUS_RATE_LIMITED_DETAIL = "Too many status requests. Try again in a moment."


def rate_limited_detail(retry_after_seconds: int) -> str:
    """The user-facing 429 message; the frontend shows it verbatim."""
    if retry_after_seconds < 60:
        unit = "second" if retry_after_seconds == 1 else "seconds"
        wait = f"{retry_after_seconds} {unit}"
    else:
        minutes = -(-retry_after_seconds // 60)  # ceiling division
        wait = "about a minute" if minutes == 1 else f"about {minutes} minutes"
    return f"Too many confirmation requests. Try again in {wait}."


@router.post(
    "/submissions",
    status_code=HTTPStatus.CREATED,
    summary="Store a submission and send its confirmation email",
    response_description=(
        "Mailjet accepted the confirmation email for sending. That is not proof of delivery "
        "to the recipient's mail server, let alone of Inbox placement. The body carries a "
        "message and the opaque tracking id for the status endpoint."
    ),
    responses={
        HTTPStatus.TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": (
                "Rate limit exceeded for this client or destination address; nothing was "
                "stored or sent. The Retry-After header gives the wait in seconds."
            ),
        },
        HTTPStatus.BAD_GATEWAY: {
            "model": ErrorResponse,
            "description": "The submission was stored but Mailjet did not accept the email.",
        },
        HTTPStatus.SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "The database is unavailable.",
        },
    },
)
async def create_submission(
    payload: SubmissionCreate,
    client_ip: ClientIpDep,
    rate_limiter: RateLimiterDep,
    session: SessionDep,
    email_service: EmailServiceDep,
) -> SubmissionCreatedResponse:
    # Rejected requests must leave no trace: no row, no Mailjet call.
    retry_after = rate_limiter.try_acquire(client_ip=client_ip, email=payload.email)
    if retry_after is not None:
        logger.warning(
            "rate limit rejected submission client_ip=%s retry_after_s=%d",
            client_ip or "unknown",
            retry_after,
        )
        raise HTTPException(
            HTTPStatus.TOO_MANY_REQUESTS,
            detail=rate_limited_detail(retry_after),
            headers={"Retry-After": str(retry_after)},
        )

    # Stage timings, so the request log shows where the time went: our database or Mailjet.
    started = time.perf_counter()
    timings: dict[str, float] = {}

    def timed(stage: str, since: float) -> None:
        timings[stage] = (time.perf_counter() - since) * 1000

    # Commit before contacting Mailjet so the attempt is recorded even if the send fails.
    submission = Submission(name=payload.name, email=payload.email)
    session.add(submission)
    stage = time.perf_counter()
    await session.commit()
    timed("db_create_duration_ms", stage)
    # Read once, while the row is loaded: a commit that fails inside its flush rolls back and
    # expires every attribute, and reading one afterwards raises instead of logging.
    submission_id = submission.id
    logger.info(
        "submission created submission_id=%s recipient=%s",
        submission_id,
        mask_email(submission.email),
    )

    try:
        stage = time.perf_counter()
        try:
            message_id = await email_service.send_confirmation(
                recipient_name=submission.name, recipient_email=submission.email
            )
        except EmailDeliveryError as exc:
            timed("mailjet_request_duration_ms", stage)
            submission.email_status = EmailStatus.FAILED
            stage = time.perf_counter()
            await session.commit()
            timed("db_status_update_duration_ms", stage)
            # error, not exception: an unreachable or refusing provider is an expected
            # failure and its reason is already logged by the email service.
            logger.error(  # noqa: TRY400
                "email delivery failed submission_id=%s email_status=%s",
                submission_id,
                EmailStatus.FAILED,
            )
            raise HTTPException(
                HTTPStatus.BAD_GATEWAY, detail=EMAIL_FAILED_DETAIL
            ) from exc
        timed("mailjet_request_duration_ms", stage)

        # Accepted, not delivered: Mailjet has the message; the recipient's server does not
        # yet. The request ends here; later states are stored by the background reconciler
        # (app.services.delivery_reconciler).
        submission.email_status = EmailStatus.ACCEPTED
        submission.mailjet_message_id = message_id
        stage = time.perf_counter()
        try:
            await session.commit()
        except SQLAlchemyError:
            # The email is already on its way but the row still says "pending". Name the
            # discrepancy here so it can be reconciled; the generic handler answers 503.
            logger.warning(
                "confirmation email accepted but status could not be updated "
                "submission_id=%s",
                submission_id,
            )
            raise
        timed("db_status_update_duration_ms", stage)
        logger.info(
            "delivery_status_updated submission_id=%s previous_status=%s new_status=%s "
            "final=false source=send_api",
            submission_id,
            EmailStatus.PENDING,
            EmailStatus.ACCEPTED,
        )
    finally:
        # Logged on every outcome once a row exists; a stage that never ran is omitted.
        logger.info(
            "submission timings submission_id=%s %s duration_ms=%.1f",
            submission_id,
            " ".join(f"{name}={value:.1f}" for name, value in timings.items()),
            (time.perf_counter() - started) * 1000,
        )

    return SubmissionCreatedResponse(
        detail=EMAIL_ACCEPTED_DETAIL, tracking_id=submission_id
    )


def parse_tracking_id(value: str) -> uuid.UUID | None:
    """The id as a UUIDv7, or ``None`` for anything this service could not have issued.

    Only the exact canonical string the POST returned is accepted: ``uuid.UUID`` would also
    take braces, a ``urn:uuid:`` prefix, upper case or no hyphens.
    """
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if parsed.version == 7 and str(parsed) == value else None


@router.get(
    "/submissions/{tracking_id}/status",
    summary="Read the delivery state of one submission",
    response_description=(
        "The delivery state as last stored in the database. Read from MySQL only: this "
        "never contacts Mailjet. The background reconciler moves the state on."
    ),
    responses={
        HTTPStatus.NOT_FOUND: {
            "model": ErrorResponse,
            "description": "No submission has this tracking id, or it is malformed.",
        },
        HTTPStatus.TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "Rate limit exceeded for this client. See Retry-After.",
        },
        HTTPStatus.SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "The database is unavailable.",
        },
    },
)
async def read_delivery_status(
    tracking_id: str,
    response: Response,
    client_ip: ClientIpDep,
    rate_limiter: StatusRateLimiterDep,
    session: SessionDep,
) -> DeliveryStatusResponse:
    # Counted before the lookup, so misses cost a caller the same as hits.
    retry_after = rate_limiter.try_acquire(client_ip)
    if retry_after is not None:
        raise HTTPException(
            HTTPStatus.TOO_MANY_REQUESTS,
            detail=STATUS_RATE_LIMITED_DETAIL,
            headers={"Retry-After": str(retry_after)},
        )
    submission_id = parse_tracking_id(tracking_id)
    # Only the status column is read: nothing else of the row reaches this handler.
    status = (
        None
        if submission_id is None
        else await session.scalar(
            select(Submission.email_status).where(Submission.id == submission_id)
        )
    )
    if status is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, detail=TRACKING_ID_NOT_FOUND_DETAIL)
    # A state that changes under the reader: no browser or proxy may answer from a copy.
    response.headers["Cache-Control"] = "no-store"
    return DeliveryStatusResponse(status=status)
