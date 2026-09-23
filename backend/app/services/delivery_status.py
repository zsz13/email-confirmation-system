"""Mailjet delivery state: what Mailjet reports, what it means here, and how a row moves.

Mailjet accepting ``POST /v3.1/send`` makes a message ``accepted``, nothing more. What happens
next is reported by Mailjet in two forms, and both are translated here into
:class:`~app.models.submission.EmailStatus`:

* the ``Status`` of the message resource, ``GET /v3/REST/message/{MessageID}``, which is
  polled because a webhook cannot reach a laptop. :func:`check_submission` is the one
  lookup-and-store step; the background reconciler (``app.services.delivery_reconciler``)
  runs it for every message still in progress, and ``python -m app.check_delivery`` (and
  with it the real-email E2E test) runs it for one;
* the events of the Event API (webhooks): ``sent``, ``open``, ``click``, ``bounce``,
  ``blocked``, ``spam`` and ``unsub``. No webhook is wired up yet. One would find the row by
  ``Submission.mailjet_message_id``, translate the event with :func:`status_from_event` and
  store it with :func:`record_status`, exactly as the poller does.

Mailjet's ``sent`` (shown as "Delivered" in its dashboard) means the recipient's mail server
accepted the message. It does not mean the message reached the Inbox rather than a spam
folder, and nothing here claims it does.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any, cast

import httpx
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import Settings
from app.models.submission import EmailStatus, Submission

logger = logging.getLogger(__name__)

# States nothing further is expected to change. Delivered can still turn into a bounce or a
# block (a server may accept a message and bounce it later); a message that was not
# delivered never turns into a delivered one, and no final state is replaced by an
# in-progress one.
TERMINAL = frozenset(
    {
        EmailStatus.DELIVERED,
        EmailStatus.SOFT_BOUNCED,
        EmailStatus.HARD_BOUNCED,
        EmailStatus.BLOCKED,
        EmailStatus.FAILED,
    }
)
IN_PROGRESS = (EmailStatus.ACCEPTED, EmailStatus.QUEUED, EmailStatus.RETRYING)
UNDELIVERED = TERMINAL - {EmailStatus.DELIVERED}

# The message resource's ``Status`` values. Opens, clicks, spam complaints and unsubscribes
# all happen after the recipient's server took the message, so they imply delivery.
MESSAGE_STATUSES: Mapping[str, EmailStatus] = {
    "queued": EmailStatus.QUEUED,
    "deferred": EmailStatus.RETRYING,  # temporary failure; Mailjet retries for up to 24 h
    "sent": EmailStatus.DELIVERED,
    "opened": EmailStatus.DELIVERED,
    "clicked": EmailStatus.DELIVERED,
    "spam": EmailStatus.DELIVERED,
    "unsub": EmailStatus.DELIVERED,
    "softbounced": EmailStatus.SOFT_BOUNCED,  # final: the retries ran out
    "hardbounced": EmailStatus.HARD_BOUNCED,
    # An unqualified bounce is not assumed permanent: "hard" is the stronger claim (the
    # address is bad) and needs Mailjet to make it.
    "bounce": EmailStatus.SOFT_BOUNCED,
    "blocked": EmailStatus.BLOCKED,
}

# Event API event types. ``bounce`` is split by the event's own ``hard_bounce`` flag.
EVENT_STATUSES: Mapping[str, EmailStatus] = {
    "sent": EmailStatus.DELIVERED,
    "open": EmailStatus.DELIVERED,
    "click": EmailStatus.DELIVERED,
    "spam": EmailStatus.DELIVERED,
    "unsub": EmailStatus.DELIVERED,
    "blocked": EmailStatus.BLOCKED,
}


class MailjetStatusError(Exception):
    """The status cannot be looked up at all: asking again would get the same refusal."""


class MailjetStatusUnavailableError(Exception):
    """No answer this time (unreachable, busy, malformed reply); asking again may get one."""


def status_from_message(value: object) -> EmailStatus | None:
    """The state a message resource ``Status`` means; ``None`` for one that says nothing.

    ``unknown``, and any value Mailjet may add later, leave the stored state as it is.
    """
    if not isinstance(value, str):
        return None
    normalised = value.strip().lower()
    status = MESSAGE_STATUSES.get(normalised)
    if status is None:
        # "unknown" is an ordinary answer while Mailjet has nothing to report; polling sees
        # it every cycle, so only a value Mailjet has newly invented is worth an INFO line.
        level = logging.DEBUG if normalised == "unknown" else logging.INFO
        logger.log(level, "unrecognised Mailjet message status %r ignored", value[:40])
    return status


def status_from_event(event: Mapping[str, object]) -> EmailStatus | None:
    """The state one Event API event means; ``None`` for an event that says nothing."""
    kind = event.get("event")
    if kind == "bounce" and event.get("blocked") is True:
        return EmailStatus.BLOCKED
    if kind == "bounce":
        hard = event.get("hard_bounce") is True
        return EmailStatus.HARD_BOUNCED if hard else EmailStatus.SOFT_BOUNCED
    return EVENT_STATUSES.get(kind) if isinstance(kind, str) else None


def next_status(current: EmailStatus, observed: EmailStatus | None) -> EmailStatus:
    """Where a row goes when Mailjet reports ``observed``.

    Reports can arrive late or out of order (a delayed ``deferred`` after ``sent``, an
    ``open`` retried after a ``bounce``), so a final state is never replaced by an
    in-progress one, and an undelivered one is never replaced by ``delivered``.
    """
    if observed is None or current in UNDELIVERED:
        return current
    if current in TERMINAL and observed not in UNDELIVERED:
        return current
    return observed


async def record_status(
    session: AsyncSession,
    submission: Submission,
    observed: EmailStatus | None,
    *,
    source: str,
    provider_duration_ms: float | None = None,
) -> bool:
    """Store what Mailjet reported, if it changes anything. True when the row changed.

    More than one writer can follow the same row (the background reconciler and a manual
    ``app.check_delivery`` run, later a webhook), so the transition is decided against the
    row as it is now, not as this session last saw it, and stored only if nobody moved it in
    between: a final state someone else stored is never overwritten from a stale copy.
    """
    await session.refresh(submission, ["email_status"])
    previous = submission.email_status
    status = next_status(previous, observed)
    if status == previous:
        # Ends the read transaction too, so the next refresh sees other writers' commits.
        await session.commit()
        return False
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Submission)
            .where(Submission.id == submission.id, Submission.email_status == previous)
            .values(email_status=status)
            .execution_options(synchronize_session=False)
        ),
    )
    await session.commit()
    if result.rowcount != 1:
        # Another writer got there first: report what it stored, not the stale copy.
        await session.refresh(submission, ["email_status"])
        await session.commit()
        return False
    set_committed_value(submission, "email_status", status)
    logger.info(
        "delivery_status_updated submission_id=%s previous_status=%s new_status=%s "
        "final=%s source=%s provider_duration_ms=%s",
        submission.id,
        previous,
        status,
        str(status in TERMINAL).lower(),
        source,
        "-" if provider_duration_ms is None else f"{provider_duration_ms:.1f}",
    )
    if status in UNDELIVERED:
        logger.warning(
            "email not delivered submission_id=%s email_status=%s",
            submission.id,
            status,
        )
    return True


class MailjetStatusClient:
    """Reads the ``Status`` of one message from Mailjet's message resource."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._url = str(settings.mailjet_message_url).rstrip("/")
        self._auth = httpx.BasicAuth(
            settings.mailjet_api_key.get_secret_value(),
            settings.mailjet_secret_key.get_secret_value(),
        )

    async def fetch(self, message_id: int) -> object:
        """The raw ``Status``, or ``None`` when Mailjet has nothing (yet) to say.

        A message may not be queryable the instant it is accepted (404): that is ``None``.
        A busy or briefly unreachable API (429, 5xx, network errors) or a reply that cannot
        be read raises :class:`MailjetStatusUnavailableError`: no answer this time, ask again.
        Any other client error (refused credentials, a malformed request) raises
        :class:`MailjetStatusError`: asking again would get the same answer. Neither
        exception carries the provider's text.
        """
        try:
            response = await self._client.get(
                f"{self._url}/{message_id}", auth=self._auth
            )
        except httpx.HTTPError as exc:
            # from None: the original exception can hold the request (and its
            # Authorization header); only its type is kept.
            raise MailjetStatusUnavailableError(type(exc).__name__) from None
        if response.is_client_error and response.status_code not in {
            HTTPStatus.NOT_FOUND,
            HTTPStatus.TOO_MANY_REQUESTS,
        }:
            raise MailjetStatusError(
                f"Mailjet refused the status lookup (HTTP {response.status_code})"
            )
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        if response.status_code != HTTPStatus.OK:
            # The body is not kept: it is the provider's text, not ours.
            raise MailjetStatusUnavailableError(f"HTTP {response.status_code}")
        try:
            return response.json()["Data"][0]["Status"]
        except ValueError, KeyError, IndexError, TypeError:
            # from None: a JSON error holds the whole provider body.
            raise MailjetStatusUnavailableError("unexpected response body") from None


async def check_submission(
    session: AsyncSession,
    submission: Submission,
    mailjet: MailjetStatusClient,
    *,
    source: str,
) -> bool:
    """Ask Mailjet about one submission's message and store what it means.

    The single lookup-and-transition step behind both the background reconciler and
    ``python -m app.check_delivery``. True when the row changed. A lookup that got no
    answer this time is logged and changes nothing; one Mailjet refused is logged and
    raises :class:`MailjetStatusError`.
    """
    message_id = submission.mailjet_message_id
    if message_id is None:
        raise ValueError(f"submission {submission.id} has no Mailjet MessageID")
    previous = submission.email_status
    # End the read that loaded the row: no transaction stays open across the HTTP call, and
    # under MySQL's REPEATABLE READ the transition below is decided on a fresh read of the
    # row rather than on the snapshot taken before the lookup.
    await session.commit()
    started = time.perf_counter()
    try:
        raw = await mailjet.fetch(message_id)
    except (MailjetStatusError, MailjetStatusUnavailableError) as exc:
        logger.warning(
            "delivery_status_check_failed submission_id=%s previous_status=%s "
            "provider_duration_ms=%.1f error=%s source=%s",
            submission.id,
            previous,
            (time.perf_counter() - started) * 1000,
            exc,
            source,
        )
        if isinstance(exc, MailjetStatusError):
            raise
        # Nothing to store, but the caller still gets the row as it is now.
        await record_status(session, submission, None, source=source)
        return False
    duration_ms = (time.perf_counter() - started) * 1000
    observed = status_from_message(raw)
    changed = await record_status(
        session, submission, observed, source=source, provider_duration_ms=duration_ms
    )
    if not changed:
        logger.debug(
            "delivery_status_checked submission_id=%s status=%s observed=%s "
            "provider_duration_ms=%.1f source=%s",
            submission.id,
            submission.email_status,
            observed or "-",
            duration_ms,
            source,
        )
    return changed


async def poll_until_final(
    check: Callable[[], Awaitable[EmailStatus]],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], Awaitable[object]],
    clock: Callable[[], float],
) -> EmailStatus:
    """Call ``check`` every ``interval_seconds`` until it returns a final state.

    Returns the last state seen: a final one, or an in-progress one when the time ran out
    first. Not ``asyncio.timeout``: running out of time is an answer here, not an error.
    ``check`` always runs at least once.
    """
    deadline = clock() + timeout_seconds
    while True:
        status = await check()
        if status in TERMINAL or clock() + interval_seconds > deadline:
            return status
        await sleep(interval_seconds)
