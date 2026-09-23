"""Rate limiting of POST /api/v1/submissions: the limiter itself, then its effect on the API."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.routes.submissions import rate_limited_detail
from app.services.rate_limit import RateLimit, SubmissionRateLimiter, normalize_email
from tests.conftest import RECIPIENT, VALID_PAYLOAD, FakeEmailService, count_submissions

IP_LIMIT = 5
EMAIL_LIMIT = 3


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> SubmissionRateLimiter:
    return SubmissionRateLimiter(
        per_ip=RateLimit(IP_LIMIT, 60),
        per_email=RateLimit(EMAIL_LIMIT, 600),
        clock=clock,
    )


def test_ip_limit_blocks_the_sixth_request_and_reports_the_wait(
    limiter: SubmissionRateLimiter, clock: FakeClock
) -> None:
    for index in range(IP_LIMIT):
        assert (
            limiter.try_acquire(client_ip="10.0.0.1", email=f"u{index}@example.com")
            is None
        )

    clock.now += 12.5
    retry_after = limiter.try_acquire(client_ip="10.0.0.1", email="u9@example.com")

    # 60s window opened at t=1000, checked at t=1012.5: 47.5s left, rounded up.
    assert retry_after == 48


def test_rejected_requests_do_not_extend_the_wait(
    limiter: SubmissionRateLimiter, clock: FakeClock
) -> None:
    for index in range(IP_LIMIT):
        limiter.try_acquire(client_ip="10.0.0.1", email=f"u{index}@example.com")
    clock.now += 30
    limiter.try_acquire(client_ip="10.0.0.1", email="late@example.com")

    clock.now += 30
    assert limiter.try_acquire(client_ip="10.0.0.1", email="late@example.com") is None


def test_window_slides_rather_than_resets(
    limiter: SubmissionRateLimiter, clock: FakeClock
) -> None:
    limiter.try_acquire(client_ip="10.0.0.1", email="first@example.com")
    clock.now += 40
    for index in range(IP_LIMIT - 1):
        limiter.try_acquire(client_ip="10.0.0.1", email=f"u{index}@example.com")

    # The first request has now left the 60s window, the other four have not.
    clock.now += 21
    assert limiter.try_acquire(client_ip="10.0.0.1", email="a@example.com") is None
    # The four requests from t+40 are now the oldest in the window: 60 - 21 = 39s left.
    assert limiter.try_acquire(client_ip="10.0.0.1", email="b@example.com") == 39


def test_email_limit_applies_across_clients_and_ignores_domain_case_and_whitespace(
    limiter: SubmissionRateLimiter,
) -> None:
    spellings = ["person@Example.com", "person@example.com", " person@EXAMPLE.COM "]
    for index, spelling in enumerate(spellings):
        assert limiter.try_acquire(client_ip=f"10.0.0.{index}", email=spelling) is None

    assert limiter.try_acquire(client_ip="10.0.0.9", email="person@example.com") == 600
    # A different address from the same fresh client is unaffected.
    assert limiter.try_acquire(client_ip="10.0.0.9", email="other@example.com") is None


@pytest.mark.parametrize(
    "spelling",
    [
        "johnsmith@gmail.com",
        "John.Smith+demo@gmail.com",
        "JOHNSMITH@GMAIL.COM",
        "j.o.h.n.s.m.i.t.h@gmail.com",
        "johnsmith+newsletter@googlemail.com",
        " john.smith@googlemail.com ",
    ],
)
def test_gmail_aliases_share_one_rate_limit_key(spelling: str) -> None:
    assert normalize_email(spelling) == "johnsmith@gmail.com"


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        # Whitespace is trimmed and the domain is case-insensitive everywhere...
        (" person@Example.COM ", "person@example.com"),
        # ...but outside Gmail the local part is kept exactly as written: no such aliasing
        # rule holds for other providers, and sub-addresses may be separate mailboxes.
        ("user+one@example.com", "user+one@example.com"),
        ("user+two@example.com", "user+two@example.com"),
        ("first.last@example.com", "first.last@example.com"),
        ("Person@example.com", "Person@example.com"),
    ],
)
def test_non_gmail_addresses_keep_their_local_part(
    spelling: str, expected: str
) -> None:
    assert normalize_email(spelling) == expected


def test_non_gmail_plus_tags_are_distinct_mailboxes(
    limiter: SubmissionRateLimiter,
) -> None:
    assert normalize_email("user+one@example.com") != normalize_email(
        "user+two@example.com"
    )
    for _ in range(EMAIL_LIMIT):
        assert limiter.try_acquire(client_ip=None, email="user+one@example.com") is None

    assert limiter.try_acquire(client_ip=None, email="user+one@example.com") == 600
    assert limiter.try_acquire(client_ip=None, email="user+two@example.com") is None


def test_unknown_client_address_skips_the_ip_window_but_keeps_the_email_window(
    limiter: SubmissionRateLimiter,
) -> None:
    for index in range(IP_LIMIT + 2):
        assert (
            limiter.try_acquire(client_ip=None, email=f"u{index}@example.com") is None
        )
    for _ in range(EMAIL_LIMIT):
        assert limiter.try_acquire(client_ip=None, email="same@example.com") is None

    assert limiter.try_acquire(client_ip=None, email="same@example.com") == 600


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (1, "Try again in 1 second."),
        (44, "Try again in 44 seconds."),
        (60, "Try again in about a minute."),
        (540, "Try again in about 9 minutes."),
    ],
)
def test_rate_limited_detail_spells_out_the_wait(seconds: int, expected: str) -> None:
    assert rate_limited_detail(seconds).endswith(expected)


def test_wait_is_at_least_one_second(
    limiter: SubmissionRateLimiter, clock: FakeClock
) -> None:
    for index in range(IP_LIMIT):
        limiter.try_acquire(client_ip="10.0.0.1", email=f"u{index}@example.com")
    clock.now += 59.9

    assert limiter.try_acquire(client_ip="10.0.0.1", email="x@example.com") == 1


def client_from(app: FastAPI, ip: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_api_returns_429_with_retry_after_once_the_ip_limit_is_reached(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(IP_LIMIT):
        response = await client.post(
            "/api/v1/submissions",
            json={"name": "Daniyar", "email": f"u{index}@example.com"},
        )
        assert response.status_code == 201

    response = await client.post(
        "/api/v1/submissions", json={"name": "Daniyar", "email": "u9@example.com"}
    )

    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert response.json() == {"detail": rate_limited_detail(retry_after)}
    # The rejected request reached neither Mailjet nor the database.
    assert len(fake_email_service.sent) == IP_LIMIT
    assert await count_submissions(session_factory) == IP_LIMIT


async def test_api_limits_one_destination_address_across_clients(
    app: FastAPI,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(EMAIL_LIMIT):
        async with client_from(app, f"10.1.0.{index}") as client:
            response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)
        assert response.status_code == 201

    async with client_from(app, "10.1.0.99") as client:
        rejected = await client.post(
            "/api/v1/submissions",
            # Same mailbox: only the domain's case differs.
            json={
                "name": "Daniyar",
                "email": RECIPIENT.replace("@mailinator.com", "@MAILINATOR.COM"),
            },
        )
        other = await client.post(
            "/api/v1/submissions",
            json={"name": "Daniyar", "email": "someone-else@example.com"},
        )

    assert rejected.status_code == 429
    assert 1 <= int(rejected.headers["Retry-After"]) <= 600
    assert other.status_code == 201
    assert len(fake_email_service.sent) == EMAIL_LIMIT + 1
    assert await count_submissions(session_factory) == EMAIL_LIMIT + 1


async def test_api_counts_gmail_aliases_as_one_mailbox(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aliases = ["John.Smith+demo@gmail.com", "johnsmith@gmail.com", "j.smith@gmail.com"]
    # The first two are one Gmail mailbox; the third is a different one.
    for alias in aliases[:2]:
        response = await client.post(
            "/api/v1/submissions", json={"name": "John", "email": alias}
        )
        assert response.status_code == 201
    response = await client.post(
        "/api/v1/submissions",
        json={"name": "John", "email": "JOHNSMITH@googlemail.com"},
    )
    assert response.status_code == 201

    rejected = await client.post(
        "/api/v1/submissions", json={"name": "John", "email": "john.smith@gmail.com"}
    )
    other = await client.post(
        "/api/v1/submissions", json={"name": "John", "email": aliases[2]}
    )

    assert rejected.status_code == 429
    assert other.status_code == 201
    # Every accepted request was delivered to the address exactly as submitted.
    assert [email for _, email in fake_email_service.sent] == [
        "John.Smith+demo@gmail.com",
        "johnsmith@gmail.com",
        "JOHNSMITH@googlemail.com",
        "j.smith@gmail.com",
    ]
    assert await count_submissions(session_factory) == 4


async def test_api_keeps_non_gmail_plus_tags_distinct(
    client: httpx.AsyncClient, fake_email_service: FakeEmailService
) -> None:
    for _ in range(EMAIL_LIMIT):
        response = await client.post(
            "/api/v1/submissions",
            json={"name": "Daniyar", "email": "user+one@example.com"},
        )
        assert response.status_code == 201

    rejected = await client.post(
        "/api/v1/submissions", json={"name": "Daniyar", "email": "user+one@example.com"}
    )
    other = await client.post(
        "/api/v1/submissions", json={"name": "Daniyar", "email": "user+two@example.com"}
    )

    assert rejected.status_code == 429
    assert other.status_code == 201
    assert fake_email_service.sent[-1] == ("Daniyar", "user+two@example.com")


async def test_rate_limit_error_is_readable_cross_origin(
    client: httpx.AsyncClient,
) -> None:
    async def post(index: int) -> httpx.Response:
        return await client.post(
            "/api/v1/submissions",
            json={"name": "Daniyar", "email": f"u{index}@example.com"},
            headers={"Origin": "http://localhost:4200"},
        )

    for index in range(IP_LIMIT):
        await post(index)
    response = await post(IP_LIMIT)

    assert response.status_code == 429
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert "Retry-After" in response.headers["access-control-expose-headers"]


async def test_concurrent_requests_cannot_exceed_the_limit(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/submissions",
                json={"name": "Daniyar", "email": f"u{index}@example.com"},
            )
            for index in range(IP_LIMIT + 7)
        )
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201] * IP_LIMIT + [429] * 7
    assert len(fake_email_service.sent) == IP_LIMIT
    assert await count_submissions(session_factory) == IP_LIMIT


async def test_concurrent_aliases_of_one_mailbox_cannot_exceed_its_limit(
    app: FastAPI,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    spellings = [
        "johnsmith@gmail.com",
        "John.Smith+a@gmail.com",
        "JOHNSMITH@googlemail.com",
        "j.o.h.n.smith+b@gmail.com",
        "johnsmith+c@gmail.com",
        "john.smith@googlemail.com",
        "johnsmith@GMAIL.COM",
    ]

    async def post(index: int, email: str) -> httpx.Response:
        # A different client address per request: only the mailbox window is shared.
        async with client_from(app, f"10.2.0.{index}") as client:
            return await client.post(
                "/api/v1/submissions", json={"name": "John", "email": email}
            )

    responses = await asyncio.gather(
        *(post(index, email) for index, email in enumerate(spellings))
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201] * EMAIL_LIMIT + [429] * (len(spellings) - EMAIL_LIMIT)
    assert len(fake_email_service.sent) == EMAIL_LIMIT
    assert await count_submissions(session_factory) == EMAIL_LIMIT


async def test_health_is_not_rate_limited(client: httpx.AsyncClient) -> None:
    for _ in range(IP_LIMIT + 5):
        response = await client.get("/api/v1/health")
        assert response.status_code == 200
