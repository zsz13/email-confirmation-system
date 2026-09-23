"""Mailjet delivery state: the mapping, the transitions, the lookup and the E2E poller.

Mailjet is never contacted: the status lookup runs through ``httpx.MockTransport``.
"""

import base64
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.check_delivery import (
    EXIT_CANNOT_CHECK,
    EXIT_DELIVERED,
    EXIT_TIMEOUT,
    EXIT_UNDELIVERED,
    check_delivery,
)
from app.core.config import Settings
from app.models.submission import EmailStatus, Submission
from app.services.delivery_status import (
    TERMINAL,
    MailjetStatusClient,
    MailjetStatusError,
    MailjetStatusUnavailableError,
    next_status,
    poll_until_final,
    record_status,
    status_from_event,
    status_from_message,
)

MESSAGE_ID = 288230376151711744
MESSAGE_URL = f"https://api.mailjet.com/v3/REST/message/{MESSAGE_ID}"


def message_body(status: str) -> dict[str, object]:
    """The shape of ``GET /v3/REST/message/{id}``, trimmed to what is read."""
    return {"Count": 1, "Data": [{"ID": MESSAGE_ID, "Status": status}], "Total": 1}


# --- mapping Mailjet's vocabulary --------------------------------------------------------


@pytest.mark.parametrize(
    ("mailjet", "expected"),
    [
        ("queued", EmailStatus.QUEUED),
        ("deferred", EmailStatus.RETRYING),
        ("sent", EmailStatus.DELIVERED),
        ("opened", EmailStatus.DELIVERED),
        ("clicked", EmailStatus.DELIVERED),
        ("spam", EmailStatus.DELIVERED),
        ("unsub", EmailStatus.DELIVERED),
        ("softbounced", EmailStatus.SOFT_BOUNCED),
        ("hardbounced", EmailStatus.HARD_BOUNCED),
        ("bounce", EmailStatus.SOFT_BOUNCED),
        ("blocked", EmailStatus.BLOCKED),
        ("SENT", EmailStatus.DELIVERED),
    ],
)
def test_message_statuses_map_onto_the_internal_model(
    mailjet: str, expected: EmailStatus
) -> None:
    assert status_from_message(mailjet) is expected


@pytest.mark.parametrize("value", ["unknown", "teleported", "", None, 2, ["sent"]])
def test_an_unknown_or_malformed_message_status_says_nothing(value: object) -> None:
    assert status_from_message(value) is None


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"event": "sent", "MessageID": MESSAGE_ID}, EmailStatus.DELIVERED),
        ({"event": "open"}, EmailStatus.DELIVERED),
        ({"event": "click"}, EmailStatus.DELIVERED),
        ({"event": "spam"}, EmailStatus.DELIVERED),
        ({"event": "unsub"}, EmailStatus.DELIVERED),
        ({"event": "bounce", "hard_bounce": True}, EmailStatus.HARD_BOUNCED),
        ({"event": "bounce", "hard_bounce": False}, EmailStatus.SOFT_BOUNCED),
        ({"event": "bounce"}, EmailStatus.SOFT_BOUNCED),
        ({"event": "bounce", "hard_bounce": "true"}, EmailStatus.SOFT_BOUNCED),
        ({"event": "blocked", "error": "preblocked"}, EmailStatus.BLOCKED),
        (
            {"event": "bounce", "blocked": True, "hard_bounce": True},
            EmailStatus.BLOCKED,
        ),
        ({"event": "something-new"}, None),
        ({"event": 7}, None),
        ({}, None),
    ],
)
def test_webhook_events_map_onto_the_internal_model(
    event: dict[str, object], expected: EmailStatus | None
) -> None:
    assert status_from_event(event) is expected


# --- how a row moves ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "observed", "expected"),
    [
        (EmailStatus.ACCEPTED, EmailStatus.QUEUED, EmailStatus.QUEUED),
        (EmailStatus.QUEUED, EmailStatus.RETRYING, EmailStatus.RETRYING),
        (EmailStatus.RETRYING, EmailStatus.DELIVERED, EmailStatus.DELIVERED),
        (EmailStatus.ACCEPTED, EmailStatus.DELIVERED, EmailStatus.DELIVERED),
        (EmailStatus.RETRYING, EmailStatus.SOFT_BOUNCED, EmailStatus.SOFT_BOUNCED),
        (EmailStatus.ACCEPTED, EmailStatus.HARD_BOUNCED, EmailStatus.HARD_BOUNCED),
        (EmailStatus.QUEUED, EmailStatus.BLOCKED, EmailStatus.BLOCKED),
        # A server may accept a message and bounce it afterwards.
        (EmailStatus.DELIVERED, EmailStatus.HARD_BOUNCED, EmailStatus.HARD_BOUNCED),
        (EmailStatus.DELIVERED, EmailStatus.BLOCKED, EmailStatus.BLOCKED),
        # A late in-progress report never reopens a final state.
        (EmailStatus.DELIVERED, EmailStatus.RETRYING, EmailStatus.DELIVERED),
        (EmailStatus.SOFT_BOUNCED, EmailStatus.QUEUED, EmailStatus.SOFT_BOUNCED),
        # A late engagement event never turns an undelivered message into a delivered one,
        # and one undelivered verdict is not overwritten by another.
        (EmailStatus.HARD_BOUNCED, EmailStatus.DELIVERED, EmailStatus.HARD_BOUNCED),
        (EmailStatus.SOFT_BOUNCED, EmailStatus.DELIVERED, EmailStatus.SOFT_BOUNCED),
        (EmailStatus.BLOCKED, EmailStatus.DELIVERED, EmailStatus.BLOCKED),
        (EmailStatus.SOFT_BOUNCED, EmailStatus.HARD_BOUNCED, EmailStatus.SOFT_BOUNCED),
        (EmailStatus.FAILED, EmailStatus.DELIVERED, EmailStatus.FAILED),
        # Nothing to say, nothing changes.
        (EmailStatus.ACCEPTED, None, EmailStatus.ACCEPTED),
        (EmailStatus.DELIVERED, None, EmailStatus.DELIVERED),
    ],
)
def test_transitions(
    current: EmailStatus, observed: EmailStatus | None, expected: EmailStatus
) -> None:
    assert next_status(current, observed) is expected


def test_only_the_settled_states_are_final() -> None:
    assert {s for s in EmailStatus if s not in TERMINAL} == {
        EmailStatus.PENDING,
        EmailStatus.ACCEPTED,
        EmailStatus.QUEUED,
        EmailStatus.RETRYING,
    }


async def stored(
    session_factory: async_sessionmaker[AsyncSession],
    status: EmailStatus = EmailStatus.ACCEPTED,
    message_id: int | None = MESSAGE_ID,
) -> Submission:
    submission = Submission(
        name="Daniyar",
        email="daniyar@example.com",
        email_status=status,
        mailjet_message_id=message_id,
    )
    async with session_factory() as session:
        session.add(submission)
        await session.commit()
    return submission


async def status_in_db(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> EmailStatus:
    async with session_factory() as session:
        row = await session.get(Submission, submission_id)
    assert row is not None
    return row.email_status


async def test_record_status_stores_and_logs_a_change(
    session_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    submission = await stored(session_factory)

    async with session_factory() as session:
        row = await session.get(Submission, submission.id)
        assert row is not None
        with caplog.at_level(logging.INFO, logger="app"):
            changed = await record_status(
                session, row, EmailStatus.SOFT_BOUNCED, source="poll"
            )

    assert changed
    assert (
        await status_in_db(session_factory, submission.id) is EmailStatus.SOFT_BOUNCED
    )
    assert (
        f"delivery_status_updated submission_id={submission.id} "
        "previous_status=accepted new_status=soft_bounced final=true source=poll"
    ) in caplog.text
    assert any(
        r.levelno == logging.WARNING and "email not delivered" in r.getMessage()
        for r in caplog.records
    )


async def test_record_status_leaves_a_final_state_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submission = await stored(session_factory, EmailStatus.DELIVERED)

    async with session_factory() as session:
        row = await session.get(Submission, submission.id)
        assert row is not None
        changed = await record_status(session, row, EmailStatus.RETRYING, source="poll")

    assert not changed
    assert await status_in_db(session_factory, submission.id) is EmailStatus.DELIVERED


# --- looking the status up ---------------------------------------------------------------


def respond_with(*responses: httpx.Response | Exception) -> httpx.MockTransport:
    """A transport answering each request with the next response, repeating the last."""
    remaining: Iterator[httpx.Response | Exception] = iter(responses)
    last: list[httpx.Response | Exception] = []

    def handler(request: httpx.Request) -> httpx.Response:
        answer = next(remaining, None)
        if answer is None:
            answer = last[0]
        last[:] = [answer]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handler)


async def fetch_with(settings: Settings, transport: httpx.MockTransport) -> object:
    async with httpx.AsyncClient(transport=transport) as client:
        return await MailjetStatusClient(client, settings).fetch(MESSAGE_ID)


async def test_the_lookup_reads_the_message_resource_with_the_api_key(
    settings: Settings,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=message_body("sent"))

    assert await fetch_with(settings, httpx.MockTransport(handler)) == "sent"
    assert str(seen[0].url) == MESSAGE_URL
    expected = base64.b64encode(b"test-api-key:test-secret-key").decode()
    assert seen[0].headers["Authorization"] == f"Basic {expected}"


async def test_a_message_not_yet_indexed_means_nothing_to_say_yet(
    settings: Settings,
) -> None:
    answer = httpx.Response(404, json={"ErrorMessage": "Object not found"})
    assert await fetch_with(settings, respond_with(answer)) is None


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(429), id="rate-limited"),
        pytest.param(httpx.Response(503), id="unavailable"),
        pytest.param(
            httpx.Response(200, json={"Count": 0, "Data": [], "Total": 0}), id="empty"
        ),
        pytest.param(httpx.Response(200, text="<html>"), id="not-json"),
        pytest.param(httpx.ConnectError("refused"), id="unreachable"),
    ],
)
async def test_no_answer_this_time_means_ask_again(
    settings: Settings, answer: httpx.Response | Exception
) -> None:
    with pytest.raises(MailjetStatusUnavailableError) as raised:
        await fetch_with(settings, respond_with(answer))

    # Only a reason of our own wording; never the provider's text or a credential.
    assert settings.mailjet_secret_key.get_secret_value() not in str(raised.value)
    assert "<html>" not in str(raised.value)


def test_unknown_is_expected_and_logged_at_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="app"):
        assert status_from_message("unknown") is None
        assert status_from_message("teleported") is None

    levels = {r.getMessage().split()[-2]: r.levelno for r in caplog.records}
    assert levels == {"'unknown'": logging.DEBUG, "'teleported'": logging.INFO}


@pytest.mark.parametrize("code", [400, 401, 403])
async def test_a_refusal_that_asking_again_cannot_fix_stops_the_lookup(
    settings: Settings, code: int
) -> None:
    with pytest.raises(MailjetStatusError) as raised:
        await fetch_with(settings, respond_with(httpx.Response(code)))

    assert settings.mailjet_secret_key.get_secret_value() not in str(raised.value)


# --- polling -----------------------------------------------------------------------------


class FakeTime:
    """A clock that only moves when the poller sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def reporting(*statuses: EmailStatus) -> Callable[[], Awaitable[EmailStatus]]:
    remaining = list(statuses)

    async def check() -> EmailStatus:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return check


async def test_polling_stops_at_the_first_final_state() -> None:
    time = FakeTime()

    status = await poll_until_final(
        reporting(EmailStatus.ACCEPTED, EmailStatus.QUEUED, EmailStatus.DELIVERED),
        timeout_seconds=60,
        interval_seconds=2,
        sleep=time.sleep,
        clock=time.clock,
    )

    assert status is EmailStatus.DELIVERED
    assert time.sleeps == [2, 2]


async def test_polling_keeps_going_through_retries() -> None:
    time = FakeTime()

    status = await poll_until_final(
        reporting(EmailStatus.RETRYING, EmailStatus.RETRYING, EmailStatus.SOFT_BOUNCED),
        timeout_seconds=60,
        interval_seconds=1,
        sleep=time.sleep,
        clock=time.clock,
    )

    assert status is EmailStatus.SOFT_BOUNCED


async def test_polling_gives_up_at_the_timeout_with_the_last_state() -> None:
    time = FakeTime()

    status = await poll_until_final(
        reporting(EmailStatus.QUEUED),
        timeout_seconds=10,
        interval_seconds=2,
        sleep=time.sleep,
        clock=time.clock,
    )

    assert status is EmailStatus.QUEUED
    assert time.now <= 10
    assert len(time.sleeps) == 5  # checks at 0, 2, 4, 6, 8 and 10 seconds


# --- the E2E check: python -m app.check_delivery ---------------------------------------


async def run_check(
    settings: Settings,
    submission_id: uuid.UUID,
    *answers: httpx.Response | Exception,
    timeout_seconds: float = 60,
) -> int:
    time = FakeTime()
    return await check_delivery(
        settings,
        submission_id,
        timeout_seconds=timeout_seconds,
        interval_seconds=2,
        transport=respond_with(*answers),
        sleep=time.sleep,
        clock=time.clock,
    )


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["queued", "sent"], (EXIT_DELIVERED, "DELIVERED", EmailStatus.DELIVERED)),
        (
            ["deferred", "softbounced"],
            (EXIT_UNDELIVERED, "SOFT_BOUNCED", EmailStatus.SOFT_BOUNCED),
        ),
        (
            ["hardbounced"],
            (EXIT_UNDELIVERED, "HARD_BOUNCED", EmailStatus.HARD_BOUNCED),
        ),
        (["blocked"], (EXIT_UNDELIVERED, "BLOCKED", EmailStatus.BLOCKED)),
    ],
)
async def test_the_check_reports_and_stores_the_final_state(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    expected: tuple[int, str, EmailStatus],
) -> None:
    exit_code, result, final = expected
    submission = await stored(session_factory)

    code = await run_check(
        settings,
        submission.id,
        *[httpx.Response(200, json=message_body(a)) for a in answers],
    )

    output = capsys.readouterr().out
    assert code == exit_code
    assert output.splitlines()[-1].startswith(f"RESULT: {result} ")
    assert await status_in_db(session_factory, submission.id) is final


async def test_a_soft_bounce_is_never_reported_as_delivered(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    submission = await stored(session_factory)

    code = await run_check(
        settings, submission.id, httpx.Response(200, json=message_body("softbounced"))
    )

    assert code != EXIT_DELIVERED
    assert "DELIVERED" not in capsys.readouterr().out


async def test_the_check_times_out_as_pending_and_keeps_the_last_state(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    submission = await stored(session_factory)

    code = await run_check(
        settings,
        submission.id,
        httpx.Response(404),
        httpx.Response(200, json=message_body("deferred")),
        timeout_seconds=10,
    )

    assert code == EXIT_TIMEOUT
    assert (
        capsys.readouterr().out.splitlines()[-1].startswith("RESULT: TIMEOUT/PENDING")
    )
    assert await status_in_db(session_factory, submission.id) is EmailStatus.RETRYING


async def test_the_check_ignores_an_unknown_status_until_a_real_one_arrives(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submission = await stored(session_factory)

    code = await run_check(
        settings,
        submission.id,
        httpx.Response(200, json=message_body("unknown")),
        httpx.Response(200, json=message_body("something-new")),
        httpx.Response(200, json=message_body("sent")),
    )

    assert code == EXIT_DELIVERED


@pytest.mark.parametrize("has_row", [True, False])
async def test_the_check_cannot_run_without_a_row_and_a_message_id(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    has_row: bool,
) -> None:
    if has_row:
        submission_id = (await stored(session_factory, EmailStatus.FAILED, None)).id
    else:
        submission_id = uuid.uuid7()

    code = await run_check(settings, submission_id, httpx.Response(200))

    assert code == EXIT_CANNOT_CHECK
    assert "RESULT: CANNOT_CHECK" in capsys.readouterr().out


async def test_the_check_prints_no_credential(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    submission = await stored(session_factory)

    with caplog.at_level(logging.DEBUG, logger="app"):
        delivered = await run_check(
            settings, submission.id, httpx.Response(200, json=message_body("sent"))
        )
        refused = await run_check(settings, submission.id, httpx.Response(401))

    assert (delivered, refused) == (EXIT_DELIVERED, EXIT_CANNOT_CHECK)
    everything = capsys.readouterr().out + caplog.text
    for secret in (
        settings.mailjet_api_key.get_secret_value(),
        settings.mailjet_secret_key.get_secret_value(),
        settings.database_url,
        base64.b64encode(b"test-api-key:test-secret-key").decode(),
    ):
        assert secret not in everything


def test_the_exit_codes_cannot_be_mistaken_for_a_crash_or_a_usage_error() -> None:
    # Python exits 1 on an uncaught error and argparse 2 on bad arguments.
    assert {EXIT_UNDELIVERED, EXIT_TIMEOUT, EXIT_CANNOT_CHECK}.isdisjoint({1, 2})
    assert len({EXIT_DELIVERED, EXIT_UNDELIVERED, EXIT_TIMEOUT, EXIT_CANNOT_CHECK}) == 4
