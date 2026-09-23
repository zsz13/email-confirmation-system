"""The public tracking id and GET /api/v1/submissions/{tracking_id}/status.

The status endpoint is a read of one column in the database. Mailjet is contacted only by
the background reconciler, never on behalf of someone checking a status.
"""

import asyncio
import uuid
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session
from app.api.v1.routes.submissions import (
    EMAIL_ACCEPTED_DETAIL,
    STATUS_RATE_LIMITED_DETAIL,
    TRACKING_ID_NOT_FOUND_DETAIL,
)
from app.core.config import Settings
from app.main import DATABASE_UNAVAILABLE_DETAIL
from app.models.submission import EmailStatus, Submission
from app.services import delivery_status
from app.services.rate_limit import ClientRateLimiter, RateLimit
from tests.conftest import FIRST_MESSAGE_ID, RECIPIENT, VALID_PAYLOAD
from tests.test_delivery_reconciler import (
    MESSAGE_ID,
    FakeMailjet,
    Gate,
    eventually,
    running_app,
)
from tests.test_delivery_status import stored
from tests.test_rate_limit import FakeClock, client_from

STATUS_LIMIT = 60


def status_url(tracking_id: str) -> str:
    return f"/api/v1/submissions/{tracking_id}/status"


async def submit(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)
    assert response.status_code == 201
    return response.json()["tracking_id"]


async def set_status(
    session_factory: async_sessionmaker[AsyncSession],
    tracking_id: str,
    status: EmailStatus,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(Submission)
            .where(Submission.id == uuid.UUID(tracking_id))
            .values(email_status=status)
        )
        await session.commit()


# --- the tracking id handed out by POST ---------------------------------------------------


async def test_post_returns_only_the_message_and_a_uuidv7_tracking_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"detail", "tracking_id"}
    assert body["detail"] == EMAIL_ACCEPTED_DETAIL
    tracking_id = uuid.UUID(body["tracking_id"])
    assert tracking_id.version == 7
    assert tracking_id.variant == uuid.RFC_4122
    assert body["tracking_id"] == str(tracking_id)  # canonical, lower-case form


async def test_post_leaks_no_private_submission_fields(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    for private in (
        RECIPIENT,
        "Daniyar",
        str(FIRST_MESSAGE_ID),
        "created_at",
        "mailjet",
    ):
        assert private not in response.text


async def test_every_submission_gets_its_own_tracking_id(
    client: httpx.AsyncClient,
) -> None:
    first, second = await submit(client), await submit(client)

    assert first != second


# --- reading the status -------------------------------------------------------------------


async def test_a_new_submission_reads_as_accepted(client: httpx.AsyncClient) -> None:
    tracking_id = await submit(client)

    response = await client.get(status_url(tracking_id))

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    # The state changes under the reader; no cache may answer from a copy.
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("status", list(EmailStatus))
async def test_the_status_is_whatever_the_database_holds_now(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    status: EmailStatus,
) -> None:
    tracking_id = await submit(client)
    await set_status(session_factory, tracking_id, status)

    response = await client.get(status_url(tracking_id))

    assert response.status_code == 200
    assert response.json() == {"status": status.value}


async def test_the_status_response_leaks_no_private_fields(
    client: httpx.AsyncClient,
) -> None:
    tracking_id = await submit(client)

    response = await client.get(status_url(tracking_id))

    assert set(response.json()) == {"status"}
    for private in (RECIPIENT, "Daniyar", str(FIRST_MESSAGE_ID), "created_at", "name"):
        assert private not in response.text
    assert not any(str(FIRST_MESSAGE_ID) in v for v in response.headers.values())


@pytest.mark.parametrize(
    "tracking_id",
    [
        pytest.param("not-a-uuid", id="garbage"),
        pytest.param("1", id="integer-id"),
        pytest.param("0", id="zero"),
        pytest.param("x" * 500, id="very-long"),
        pytest.param("%00", id="nul"),
        pytest.param("' OR '1'='1", id="sql-ish"),
        pytest.param(str(uuid.uuid4()), id="uuid4"),
        pytest.param("00000000-0000-0000-0000-000000000000", id="nil-uuid"),
        pytest.param(str(uuid.uuid7()), id="unknown-uuid7"),
    ],
)
async def test_invalid_and_unknown_ids_get_one_indistinguishable_404(
    client: httpx.AsyncClient, tracking_id: str
) -> None:
    await submit(client)  # the table is not empty; the id still matches nothing

    response = await client.get(status_url(tracking_id))

    assert response.status_code == 404
    assert response.json() == {"detail": TRACKING_ID_NOT_FOUND_DETAIL}


@pytest.mark.parametrize(
    "spell",
    [
        pytest.param(str.upper, id="upper-case"),
        pytest.param(lambda v: v.replace("-", ""), id="no-hyphens"),
        pytest.param(lambda v: "{" + v + "}", id="braces"),
        pytest.param(lambda v: f"urn:uuid:{v}", id="urn"),
    ],
)
async def test_only_the_exact_issued_tracking_id_is_accepted(
    client: httpx.AsyncClient, spell: Callable[[str], str]
) -> None:
    tracking_id = await submit(client)

    response = await client.get(status_url(spell(tracking_id)))

    assert response.status_code == 404
    assert response.json() == {"detail": TRACKING_ID_NOT_FOUND_DETAIL}


async def test_a_database_outage_answers_503_without_internal_details(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    tracking_id = await submit(client)

    async def unavailable_session() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("mysql is down"))

    app.dependency_overrides[get_session] = unavailable_session
    response = await client.get(status_url(tracking_id))

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert "mysql is down" not in response.text


async def test_there_is_no_public_listing_or_record_endpoint(
    client: httpx.AsyncClient,
) -> None:
    tracking_id = await submit(client)

    listing = await client.get("/api/v1/submissions")
    record = await client.get(f"/api/v1/submissions/{tracking_id}")
    search = await client.get("/api/v1/submissions/status", params={"email": RECIPIENT})

    assert listing.status_code == 405  # the collection only accepts POST
    assert record.status_code == 404
    assert search.status_code == 404
    for response in (listing, record, search):
        assert RECIPIENT not in response.text


# --- the status endpoint never reaches Mailjet ---------------------------------------------


async def test_reading_a_status_makes_no_outbound_request(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracking_id = await submit(client)
    await set_status(session_factory, tracking_id, EmailStatus.QUEUED)
    outbound: list[str] = []

    async def no_mailjet(*_args: object, **_kwargs: object) -> None:
        outbound.append("called")
        raise AssertionError("the status endpoint must not contact Mailjet")

    # Every road to Mailjet: the lookup-and-store step, the HTTP lookup, and any request
    # over the network (the test client itself talks to the app in-process).
    monkeypatch.setattr(delivery_status, "check_submission", no_mailjet)
    monkeypatch.setattr(delivery_status.MailjetStatusClient, "fetch", no_mailjet)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_mailjet)

    response = await client.get(status_url(tracking_id))

    assert response.json() == {"status": "queued"}
    assert outbound == []


async def test_status_reads_the_database_while_only_the_reconciler_asks_mailjet(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mailjet would answer "sent", but holds its answer until the test releases it: until
    # then the database still says queued, and that is what every status read must return.
    gate = Gate()
    mailjet = FakeMailjet({MESSAGE_ID: gate})
    settings = settings.model_copy(update={"delivery_reconcile_interval_seconds": 1})

    async with running_app(settings, mailjet, monkeypatch) as application:
        submission = await stored(
            application.state.session_factory, EmailStatus.QUEUED, MESSAGE_ID
        )
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            url = status_url(str(submission.id))
            for _ in range(10):
                response = await client.get(url)
                assert response.json() == {"status": "queued"}
            # Ten reads, one lookup: the reconciler's own, still waiting on Mailjet.
            assert mailjet.looked_up == [MESSAGE_ID]

            gate.release.set()

            async def delivered() -> bool:
                return (await client.get(url)).json() == {"status": "delivered"}

            await eventually(delivered)


# --- rate limiting -------------------------------------------------------------------------


def test_client_limiter_allows_the_limit_then_reports_the_wait() -> None:
    clock = FakeClock()
    limiter = ClientRateLimiter(RateLimit(3, 60), clock=clock)

    assert [limiter.try_acquire("10.0.0.1") for _ in range(3)] == [None, None, None]
    clock.now += 20
    assert limiter.try_acquire("10.0.0.1") == 40
    assert limiter.try_acquire("10.0.0.2") is None  # counted per client
    assert limiter.try_acquire(None) is None  # an unknown peer is not lumped together
    clock.now += 40
    assert limiter.try_acquire("10.0.0.1") is None  # the window slid on


async def test_status_reads_are_rate_limited_per_client_without_touching_post(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    tracking_id = await submit(client)
    url = status_url(tracking_id)
    # Misses count as much as hits, so sweeping ids costs the same as polling one.
    for index in range(STATUS_LIMIT):
        target = url if index % 2 else status_url(str(uuid.uuid7()))
        assert (await client.get(target)).status_code in {200, 404}

    limited = await client.get(url)

    assert limited.status_code == 429
    assert limited.json() == {"detail": STATUS_RATE_LIMITED_DETAIL}
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    async with client_from(app, "10.9.9.9") as other:
        assert (await other.get(url)).status_code == 200
    # Polling spends the status budget, not the submission budget.
    assert (
        await client.post("/api/v1/submissions", json=VALID_PAYLOAD)
    ).status_code == 201
