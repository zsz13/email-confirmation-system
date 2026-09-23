"""The background delivery reconciler: which rows it follows, what it stores, how it runs.

Mailjet is never contacted: status lookups go through ``httpx.MockTransport``.
"""

import asyncio
import itertools
import logging
import time
import types
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from functools import partial

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import main as app_main
from app.api.deps import get_email_service
from app.api.v1.routes.submissions import EMAIL_ACCEPTED_DETAIL
from app.core.config import Settings
from app.db.base import Base
from app.main import create_app, lifespan
from app.models.submission import EmailStatus, Submission
from app.services.delivery_reconciler import DeliveryReconciler
from app.services.delivery_status import MailjetStatusClient, record_status
from tests.conftest import VALID_PAYLOAD, FakeEmailService
from tests.test_delivery_status import message_body, status_in_db, stored

MESSAGE_ID = 288230376151711744

Answer = httpx.Response | Exception | Callable[[], Awaitable[httpx.Response]]


class FakeMailjet:
    """The message resource: one scripted answer per MessageID, and a log of lookups."""

    def __init__(self, answers: Mapping[int, Answer] | None = None) -> None:
        self.answers: dict[int, Answer] = dict(answers or {})
        self.looked_up: list[int] = []
        self.transport = httpx.MockTransport(self._handle)

    def say(self, message_id: int, status: str) -> None:
        self.answers[message_id] = httpx.Response(200, json=message_body(status))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        message_id = int(request.url.path.rsplit("/", 1)[1])
        self.looked_up.append(message_id)
        answer = self.answers[message_id]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return await answer()


class Gate:
    """A lookup that does not answer until the test lets it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> httpx.Response:
        self.entered.set()
        await self.release.wait()
        return httpx.Response(200, json=message_body("sent"))


@asynccontextmanager
async def reconciler_for(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    mailjet: FakeMailjet,
    interval_seconds: float = 3.0,
) -> AsyncGenerator[DeliveryReconciler]:
    async with httpx.AsyncClient(transport=mailjet.transport) as client:
        reconciler = DeliveryReconciler(
            session_factory,
            MailjetStatusClient(client, settings),
            interval_seconds=interval_seconds,
        )
        try:
            yield reconciler
        finally:
            await reconciler.stop()


async def eventually(
    condition: Callable[[], Awaitable[bool]], within_seconds: float = 5
) -> None:
    deadline = time.monotonic() + within_seconds
    while not await condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


def reconciler_tasks() -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if t.get_name() == "delivery-reconciler"]


# --- which rows are followed -------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [EmailStatus.ACCEPTED, EmailStatus.QUEUED, EmailStatus.RETRYING]
)
async def test_an_in_progress_row_is_discovered_and_moved_on(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    status: EmailStatus,
) -> None:
    submission = await stored(session_factory, status)
    mailjet = FakeMailjet()
    mailjet.say(MESSAGE_ID, "sent")

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        assert await reconciler.run_cycle() == 1

    assert mailjet.looked_up == [MESSAGE_ID]
    assert await status_in_db(session_factory, submission.id) is EmailStatus.DELIVERED


@pytest.mark.parametrize(
    ("status", "message_id"),
    [
        (EmailStatus.DELIVERED, MESSAGE_ID),
        (EmailStatus.SOFT_BOUNCED, MESSAGE_ID),
        (EmailStatus.HARD_BOUNCED, MESSAGE_ID),
        (EmailStatus.BLOCKED, MESSAGE_ID),
        (EmailStatus.FAILED, None),
        (EmailStatus.PENDING, None),
        # In progress, but Mailjet reported no MessageID: there is nothing to look up.
        (EmailStatus.ACCEPTED, None),
        (EmailStatus.QUEUED, None),
        (EmailStatus.RETRYING, None),
    ],
)
async def test_a_final_or_unlookupable_row_is_left_alone(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    status: EmailStatus,
    message_id: int | None,
) -> None:
    submission = await stored(session_factory, status, message_id)
    # Would turn every row delivered, if it were ever asked.
    mailjet = FakeMailjet()
    mailjet.say(MESSAGE_ID, "sent")

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        assert await reconciler.run_cycle() == 0

    assert mailjet.looked_up == []
    assert await status_in_db(session_factory, submission.id) is status


# --- what is stored ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "mailjet_status", "expected"),
    [
        (EmailStatus.ACCEPTED, "queued", EmailStatus.QUEUED),
        (EmailStatus.QUEUED, "sent", EmailStatus.DELIVERED),
        (EmailStatus.ACCEPTED, "softbounced", EmailStatus.SOFT_BOUNCED),
        (EmailStatus.ACCEPTED, "deferred", EmailStatus.RETRYING),
        (EmailStatus.RETRYING, "hardbounced", EmailStatus.HARD_BOUNCED),
        (EmailStatus.QUEUED, "blocked", EmailStatus.BLOCKED),
    ],
)
async def test_the_reported_state_is_stored(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    current: EmailStatus,
    mailjet_status: str,
    expected: EmailStatus,
) -> None:
    submission = await stored(session_factory, current)
    mailjet = FakeMailjet()
    mailjet.say(MESSAGE_ID, mailjet_status)

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        await reconciler.run_cycle()

    assert await status_in_db(session_factory, submission.id) is expected


async def test_a_row_is_followed_until_final_and_then_no_longer_looked_up(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    submission = await stored(session_factory)
    mailjet = FakeMailjet()

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        with caplog.at_level(logging.DEBUG, logger="app"):
            mailjet.say(MESSAGE_ID, "queued")
            await reconciler.run_cycle()
            assert (
                await status_in_db(session_factory, submission.id) is EmailStatus.QUEUED
            )
            await reconciler.run_cycle()  # still queued: checked, nothing stored
            mailjet.say(MESSAGE_ID, "sent")
            await reconciler.run_cycle()
            assert (
                await status_in_db(session_factory, submission.id)
                is EmailStatus.DELIVERED
            )
            # Mailjet would now say something else; the row is final and is not asked about.
            mailjet.say(MESSAGE_ID, "softbounced")
            assert await reconciler.run_cycle() == 0

    assert mailjet.looked_up == [MESSAGE_ID] * 3
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("app.")]
    updated = [m for m in messages if m.startswith("delivery_status_updated ")]
    assert len(updated) == 2
    assert updated[0].startswith(
        f"delivery_status_updated submission_id={submission.id} previous_status=accepted"
        " new_status=queued final=false source=reconciler provider_duration_ms="
    )
    assert updated[1].startswith(
        f"delivery_status_updated submission_id={submission.id} previous_status=queued"
        " new_status=delivered final=true source=reconciler provider_duration_ms="
    )
    # An unchanged check is DEBUG, never INFO: steady polling does not fill the log.
    checked = [r for r in caplog.records if "delivery_status_checked" in r.getMessage()]
    assert len(checked) == 1
    assert checked[0].levelno == logging.DEBUG
    # No request started this work, so it carries no request id.
    assert {getattr(r, "request_id", "-") for r in caplog.records} == {"-"}


async def test_a_stale_copy_never_overwrites_a_final_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two writers on one row: a bounce stored by one is not replaced by the other."""
    submission = await stored(session_factory)

    async with session_factory() as slow, session_factory() as fast:
        stale = await slow.get(Submission, submission.id)
        assert stale is not None
        fresh = await fast.get(Submission, submission.id)
        assert fresh is not None
        assert await record_status(fast, fresh, EmailStatus.SOFT_BOUNCED, source="a")

        assert stale.email_status is EmailStatus.ACCEPTED  # what `slow` last saw
        assert not await record_status(slow, stale, EmailStatus.DELIVERED, source="b")

    assert (
        await status_in_db(session_factory, submission.id) is EmailStatus.SOFT_BOUNCED
    )


async def test_a_cycle_takes_a_bounded_batch_newest_first(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.delivery_reconciler.BATCH_SIZE", 2)
    for message_id in (1, 2, 3):  # stored oldest first
        await stored(session_factory, message_id=message_id)
    mailjet = FakeMailjet({i: httpx.Response(404) for i in (1, 2, 3)})

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        assert await reconciler.run_cycle() == 2

    assert mailjet.looked_up == [3, 2]


# --- failures ----------------------------------------------------------------------------


async def test_one_failed_lookup_does_not_stop_the_others(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    unreachable = await stored(session_factory, message_id=1)
    refused = await stored(session_factory, message_id=2)
    busy = await stored(session_factory, message_id=3)
    fine = await stored(session_factory, message_id=4)
    mailjet = FakeMailjet(
        {
            1: httpx.ConnectError("no route to host"),
            2: httpx.Response(401),
            3: httpx.Response(503),
            4: httpx.Response(200, json=message_body("sent")),
        }
    )

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        with caplog.at_level(logging.INFO, logger="app"):
            assert await reconciler.run_cycle() == 4

    assert sorted(mailjet.looked_up) == [1, 2, 3, 4]
    assert await status_in_db(session_factory, fine.id) is EmailStatus.DELIVERED
    for row in (unreachable, refused, busy):
        assert await status_in_db(session_factory, row.id) is EmailStatus.ACCEPTED
    # Each failure names its row, so an operator can tell which lookup went wrong.
    failed = {
        r.getMessage().split()[1]: r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("delivery_status_check_failed ")
    }
    assert set(failed) == {
        f"submission_id={row.id}" for row in (unreachable, refused, busy)
    }
    assert "error=ConnectError " in failed[f"submission_id={unreachable.id}"]
    assert "(HTTP 401)" in failed[f"submission_id={refused.id}"]
    assert "error=HTTP 503 " in failed[f"submission_id={busy.id}"]
    for message in failed.values():
        assert "previous_status=accepted provider_duration_ms=" in message


async def test_the_worker_keeps_running_through_failures(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    submission = await stored(session_factory)
    mailjet = FakeMailjet({MESSAGE_ID: httpx.Response(401)})

    async with reconciler_for(
        settings, session_factory, mailjet, interval_seconds=0.01
    ) as reconciler:
        reconciler.start()
        await eventually(lambda: _looked_up_at_least(mailjet, 2))
        assert reconciler.running
        mailjet.say(MESSAGE_ID, "sent")

        async def delivered() -> bool:
            status = await status_in_db(session_factory, submission.id)
            return status is EmailStatus.DELIVERED

        await eventually(delivered)
        assert reconciler.running


async def _looked_up_at_least(mailjet: FakeMailjet, count: int) -> bool:
    return len(mailjet.looked_up) >= count


async def test_a_database_outage_does_not_stop_the_worker(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """No tables at all: every cycle fails in its query, and the worker carries on."""
    bare = create_app(settings)
    mailjet = FakeMailjet()

    with caplog.at_level(logging.WARNING, logger="app"):
        async with reconciler_for(
            settings, bare.state.session_factory, mailjet, interval_seconds=0.01
        ) as reconciler:
            reconciler.start()

            async def failed_twice() -> bool:
                return caplog.text.count("delivery_reconciliation_failed") >= 2

            await eventually(failed_twice)
            assert reconciler.running
    await bare.state.engine.dispose()


# --- running it --------------------------------------------------------------------------


async def test_the_worker_looks_an_unfinished_row_up_once_per_interval(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await stored(session_factory)
    looked_up_at: list[float] = []

    async def nothing_to_report_yet() -> httpx.Response:
        looked_up_at.append(time.monotonic())
        return httpx.Response(404, json={"ErrorMessage": "Object not found"})

    mailjet = FakeMailjet({MESSAGE_ID: nothing_to_report_yet})
    interval = 1.0

    async def three_lookups() -> bool:
        return len(looked_up_at) >= 3

    async with reconciler_for(
        settings, session_factory, mailjet, interval_seconds=interval
    ) as reconciler:
        reconciler.start()
        await eventually(three_lookups)

    # One cycle per interval, counted from the end of the previous cycle: never sooner,
    # and no cycle skipped.
    gaps = [later - earlier for earlier, later in itertools.pairwise(looked_up_at)]
    assert all(interval - 0.01 <= gap < 2 * interval - 0.1 for gap in gaps[:2]), gaps


@pytest.mark.parametrize("interval", [3, 7])
async def test_the_application_starts_the_worker_with_the_configured_interval(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    interval: int,
) -> None:
    # 3 is the default (test_config pins it); 7 shows the setting, not a constant, is used.
    settings = settings.model_copy(
        update={"delivery_reconcile_interval_seconds": interval}
    )
    with caplog.at_level(logging.INFO, logger="app"):
        async with running_app(settings, FakeMailjet(), monkeypatch):
            pass

    assert f"delivery reconciler started interval_s={interval} " in caplog.text


async def test_cycles_never_overlap(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    submission = await stored(session_factory)
    gate = Gate()
    mailjet = FakeMailjet({MESSAGE_ID: gate})

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        first = asyncio.create_task(reconciler.run_cycle())
        await gate.entered.wait()
        # The first cycle is mid-lookup: a second one does nothing at all.
        assert await reconciler.run_cycle() == 0
        assert mailjet.looked_up == [MESSAGE_ID]
        gate.release.set()
        assert await first == 1

    assert await status_in_db(session_factory, submission.id) is EmailStatus.DELIVERED


async def test_start_is_idempotent_and_stop_cancels_a_lookup_in_flight(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    submission = await stored(session_factory)
    gate = Gate()  # never released: the lookup hangs until it is cancelled
    mailjet = FakeMailjet({MESSAGE_ID: gate})

    async with reconciler_for(settings, session_factory, mailjet) as reconciler:
        reconciler.start()
        reconciler.start()
        assert len(reconciler_tasks()) == 1
        await gate.entered.wait()

        await asyncio.wait_for(reconciler.stop(), timeout=2)

        assert not reconciler.running
        assert reconciler_tasks() == []
        await reconciler.stop()  # a second stop is harmless

    assert await status_in_db(session_factory, submission.id) is EmailStatus.ACCEPTED


async def test_stop_wins_even_when_cleanup_replaces_the_cancellation(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await stored(session_factory)
    entered = asyncio.Event()

    async def cancelled_into_another_error() -> httpx.Response:
        first = not entered.is_set()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if first:
                # What a failing cleanup looks like from the loop: not a cancellation.
                # Only once, so a regression fails the test instead of hanging it.
                raise RuntimeError("connection reset during cleanup") from None
            raise
        raise AssertionError("unreachable")

    mailjet = FakeMailjet({MESSAGE_ID: cancelled_into_another_error})
    async with reconciler_for(
        settings, session_factory, mailjet, interval_seconds=0.01
    ) as reconciler:
        reconciler.start()
        await entered.wait()
        await asyncio.wait_for(reconciler.stop(), timeout=2)

        assert reconciler_tasks() == []


# --- inside the application --------------------------------------------------------------


@asynccontextmanager
async def running_app(
    settings: Settings, mailjet: FakeMailjet, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[FastAPI]:
    """The whole application with its lifespan running, Mailjet mocked, tables created."""
    # The lifespan builds its own HTTP client; this one talks to the fake Mailjet instead.
    monkeypatch.setattr(
        app_main,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=partial(httpx.AsyncClient, transport=mailjet.transport)
        ),
    )
    application = create_app(settings)
    async with application.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    application.dependency_overrides[get_email_service] = FakeEmailService
    async with lifespan(application):
        yield application


async def test_a_restarted_backend_resumes_an_unfinished_row(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailjet = FakeMailjet()
    mailjet.say(MESSAGE_ID, "sent")
    # Left accepted by a previous process: the row is all that survives a restart.
    bootstrap = create_app(settings)
    async with bootstrap.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    submission = await stored(bootstrap.state.session_factory)
    await bootstrap.state.engine.dispose()

    async with running_app(settings, mailjet, monkeypatch) as application:
        sessions = application.state.session_factory

        async def delivered() -> bool:
            return await status_in_db(sessions, submission.id) is EmailStatus.DELIVERED

        # The first cycle runs at startup, not one interval (3 s) later.
        await eventually(delivered, within_seconds=2)
        assert application.state.delivery_reconciler.running

    assert not application.state.delivery_reconciler.running
    assert reconciler_tasks() == []


async def test_post_returns_without_waiting_for_the_delivery_lookup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake send answers with MessageID MESSAGE_ID, whose lookup hangs for as long as
    # the test runs; the shortest interval allowed, so the worker finds the row quickly.
    gate = Gate()
    mailjet = FakeMailjet({MESSAGE_ID: gate})
    settings = settings.model_copy(update={"delivery_reconcile_interval_seconds": 1})

    async with running_app(settings, mailjet, monkeypatch) as application:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await asyncio.wait_for(
                client.post("/api/v1/submissions", json=VALID_PAYLOAD), timeout=2
            )
            assert response.status_code == 201
            assert response.json()["detail"] == EMAIL_ACCEPTED_DETAIL

            # The worker picks the new row up on its own; the request is long done.
            await asyncio.wait_for(gate.entered.wait(), timeout=5)
            async with application.state.session_factory() as session:
                rows = (await session.scalars(select(Submission))).all()
            assert [row.email_status for row in rows] == [EmailStatus.ACCEPTED]
