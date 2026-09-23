"""Follow one submission's confirmation email until Mailjet reports a final state.

    python -m app.check_delivery <submission-id> [--timeout 120] [--interval 2]

Reads the Mailjet MessageID stored with the submission, polls Mailjet's message resource,
stores every change of state on the row and ends with one line, ``RESULT: <STATE>``. Each
poll is the same lookup-and-store step the running backend's background reconciler performs
(``check_submission`` in ``app.services.delivery_status``), so the two can follow one row at
the same time without disagreeing. The local real-email E2E test runs it inside the backend
container, so the Mailjet credentials never leave it; none is ever printed.

Exit status: 0 delivered (the recipient's server accepted the message, which still says
nothing about Inbox placement), 10 soft-bounced, hard-bounced, blocked or failed, 11 no final
state before the timeout, 12 the status cannot be checked at all. The non-zero codes stay
clear of Python's 1 (an uncaught error) and argparse's 2 (bad arguments), so a crash is never
mistaken for something Mailjet reported.
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.models.submission import EmailStatus, Submission
from app.services.delivery_status import (
    UNDELIVERED,
    MailjetStatusClient,
    MailjetStatusError,
    check_submission,
    poll_until_final,
)

EXIT_DELIVERED = 0
EXIT_UNDELIVERED = 10
EXIT_TIMEOUT = 11
EXIT_CANNOT_CHECK = 12

TIMEOUT_RESULT = "TIMEOUT/PENDING"


def result_of(status: EmailStatus) -> tuple[str, int]:
    """The ``RESULT`` label and exit status for the state the poll ended in."""
    if status is EmailStatus.DELIVERED:
        return "DELIVERED", EXIT_DELIVERED
    if status in UNDELIVERED:
        return status.value.upper(), EXIT_UNDELIVERED
    return TIMEOUT_RESULT, EXIT_TIMEOUT


def say(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


async def check_delivery(
    settings: Settings,
    submission_id: uuid.UUID,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Poll until a final state or the timeout; report it and return the exit status.

    ``transport``, ``sleep`` and ``clock`` exist for the tests: a mocked Mailjet and time
    that only moves when the poller waits.
    """
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with (
            sessions() as session,
            httpx.AsyncClient(
                timeout=settings.mailjet_timeout_seconds, transport=transport
            ) as client,
        ):
            submission = await session.get(Submission, submission_id)
            if submission is None:
                say(f"RESULT: CANNOT_CHECK (no submission {submission_id})")
                return EXIT_CANNOT_CHECK
            if submission.mailjet_message_id is None:
                say(
                    "RESULT: CANNOT_CHECK (no Mailjet MessageID stored; "
                    f"email_status={submission.email_status})"
                )
                return EXIT_CANNOT_CHECK

            mailjet = MailjetStatusClient(client, settings)
            say(
                f"Polling Mailjet every {interval_seconds:g}s "
                f"for up to {timeout_seconds:g}s"
            )
            say(f"  state: {submission.email_status}")

            async def check() -> EmailStatus:
                if await check_submission(session, submission, mailjet, source="poll"):
                    say(f"  state: {submission.email_status}")
                return submission.email_status

            try:
                status = await poll_until_final(
                    check,
                    timeout_seconds=timeout_seconds,
                    interval_seconds=interval_seconds,
                    sleep=sleep,
                    clock=clock,
                )
            except MailjetStatusError as exc:
                say(f"RESULT: CANNOT_CHECK ({exc})")
                return EXIT_CANNOT_CHECK
    finally:
        await engine.dispose()

    label, code = result_of(status)
    say(f"RESULT: {label} (email_status={status})")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.check_delivery",
        description="Follow a submission's email until Mailjet reports a final state.",
    )
    parser.add_argument("submission_id", type=uuid.UUID)
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds")
    arguments = parser.parse_args(argv)
    if arguments.timeout <= 0 or arguments.interval <= 0:
        parser.error("--timeout and --interval must be positive")

    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(
        check_delivery(
            settings,
            arguments.submission_id,
            timeout_seconds=arguments.timeout,
            interval_seconds=arguments.interval,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
