"""The read-only admin panel: login, what it shows, and everything it refuses to do."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.auth import LOGIN_FAILURES, THROTTLED_DETAIL
from app.core.config import (
    DEMO_ADMIN_PASSWORD,
    DEMO_ADMIN_PATH,
    DEMO_ADMIN_USERNAME,
    Settings,
)
from app.main import create_app
from app.models.submission import EmailStatus, Submission
from tests.conftest import count_submissions

ADMIN = DEMO_ADMIN_PATH
LIST = f"{ADMIN}/submission/list"
CREDENTIALS = {"username": DEMO_ADMIN_USERNAME, "password": DEMO_ADMIN_PASSWORD}
# The id of a listed row, taken from its details link: the ids also appear in the id column
# and, when the term is a UUID, in the search box, which echoes whatever was searched for.
ROW_PATTERN = re.compile(
    r"/submission/details/([0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12})"
)


async def login(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{ADMIN}/login", data=CREDENTIALS)


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The shared test client, logged in."""
    response = await login(client)
    assert response.status_code == 302
    return client


async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[tuple[str, str, EmailStatus, datetime]],
) -> list[Submission]:
    submissions = [
        Submission(name=name, email=email, email_status=status, created_at=created)
        for name, email, status, created in rows
    ]
    async with session_factory() as session:
        session.add_all(submissions)
        await session.commit()
    return submissions


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
THREE = [
    ("Alice Adams", "alice@example.com", EmailStatus.ACCEPTED, NOW - timedelta(days=2)),
    ("Bob Brown", "bob@example.org", EmailStatus.FAILED, NOW - timedelta(days=1)),
    ("Carol Clark", "carol@example.net", EmailStatus.PENDING, NOW),
]


def uuids_in(html: str) -> list[str]:
    """The ids of the rows a list page shows, in the order they are listed."""
    return list(dict.fromkeys(ROW_PATTERN.findall(html)))


# --- authentication -------------------------------------------------------------------


@pytest.mark.parametrize("path", [ADMIN, f"{ADMIN}/", LIST])
async def test_unauthenticated_access_is_redirected_to_the_login_page(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)
    if response.status_code == 308:  # the bare path redirects into the mounted panel
        assert response.headers["location"] == f"{ADMIN}/"
        response = await client.get(response.headers["location"])

    assert response.status_code == 302
    assert response.headers["location"].endswith(f"{ADMIN}/login")


@pytest.mark.parametrize(
    "form",
    [
        {"username": DEMO_ADMIN_USERNAME, "password": "wrong"},
        {"username": "someone-else", "password": DEMO_ADMIN_PASSWORD},
        {"username": "", "password": ""},
        {"username": DEMO_ADMIN_USERNAME},
    ],
)
async def test_wrong_credentials_are_rejected_and_grant_nothing(
    client: httpx.AsyncClient, form: dict[str, str]
) -> None:
    response = await client.post(f"{ADMIN}/login", data=form)

    assert response.status_code == 400
    assert "Invalid credentials" in response.text
    assert (await client.get(LIST)).status_code == 302


async def test_demo_credentials_log_in_and_the_cookie_is_scoped_to_the_panel(
    client: httpx.AsyncClient,
) -> None:
    response = await login(client)

    assert response.status_code == 302
    assert response.headers["location"].endswith(f"{ADMIN}/")
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("admin_session=")
    assert f"path={ADMIN.lower()}" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" not in cookie  # the plain-HTTP demo; see the next test

    assert (await client.get(LIST)).status_code == 200


async def test_secure_cookie_flag_follows_the_setting(settings: Settings) -> None:
    secure = settings.model_copy(update={"admin_cookie_secure": True})
    app = create_app(secure)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await login(client)
    await app.state.engine.dispose()

    assert "secure" in response.headers["set-cookie"].lower()


async def test_overridden_credentials_replace_the_demo_ones(
    settings: Settings,
) -> None:
    custom = settings.model_copy(
        update={
            "admin_username": "ops",
            "admin_password": SecretStr("a-real-password"),
            "admin_session_secret": SecretStr("a-real-session-secret"),
            "admin_path": "/ops-panel",
        }
    )
    app = create_app(custom)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        demo = await client.post("/ops-panel/login", data=CREDENTIALS)
        real = await client.post(
            "/ops-panel/login", data={"username": "ops", "password": "a-real-password"}
        )
        moved = await client.get(f"{ADMIN}/login")
        page = await client.get("/ops-panel/login")
    await app.state.engine.dispose()

    assert demo.status_code == 400
    assert real.status_code == 302
    assert moved.status_code == 404
    assert "Demonstration access only" not in page.text


async def test_demo_warning_stays_while_the_session_secret_is_the_demo_one(
    settings: Settings,
) -> None:
    half_configured = settings.model_copy(
        update={"admin_password": SecretStr("a-real-password")}
    )
    app = create_app(half_configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        page = await client.get(f"{ADMIN}/login")
    await app.state.engine.dispose()

    assert "Demonstration access only" in page.text


def test_secure_cookie_refuses_the_demo_secrets(settings: Settings) -> None:
    base = settings.model_dump()
    with pytest.raises(ValueError, match="ADMIN_COOKIE_SECURE"):
        Settings.model_validate({**base, "admin_cookie_secure": True})
    with pytest.raises(ValueError, match="ADMIN_COOKIE_SECURE"):
        Settings.model_validate(
            {**base, "admin_cookie_secure": True, "admin_password": "a-real-password"}
        )
    Settings.model_validate(
        {
            **base,
            "admin_cookie_secure": True,
            "admin_password": "a-real-password",
            "admin_session_secret": "a-real-session-secret",
        }
    )


async def test_repeated_failures_throttle_the_login(client: httpx.AsyncClient) -> None:
    for _ in range(LOGIN_FAILURES.max_requests):
        response = await client.post(
            f"{ADMIN}/login", data={"username": DEMO_ADMIN_USERNAME, "password": "x"}
        )
        assert response.status_code == 400

    throttled = await login(client)  # even the right password is refused now

    assert throttled.status_code == 429
    assert throttled.text == THROTTLED_DETAIL
    assert 1 <= int(throttled.headers["Retry-After"]) <= LOGIN_FAILURES.window_seconds
    assert (await client.get(LIST)).status_code == 302


async def test_login_page_carries_the_demo_warning(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{ADMIN}/login")

    assert response.status_code == 200
    assert "Demonstration access only" in response.text
    assert "must never be reused in production" in response.text
    assert DEMO_ADMIN_PASSWORD not in response.text


async def test_logout_ends_the_session(admin: httpx.AsyncClient) -> None:
    response = await admin.get(f"{ADMIN}/logout")

    assert response.status_code == 302
    assert (await admin.get(LIST)).status_code == 302


async def test_admin_path_must_be_plain_segments_outside_the_api(
    settings: Settings,
) -> None:
    for bad in [
        "admin",
        "/api/admin",
        "/api",
        "//evil.test",
        "/ops panel",
        "/a?b",
        "/",
    ]:
        with pytest.raises(ValueError, match="ADMIN_PATH"):
            Settings.model_validate({**settings.model_dump(), "admin_path": bad})


# --- what the panel shows --------------------------------------------------------------


async def test_dashboard_shows_summary_cards(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)
    old = datetime.now(UTC) - timedelta(days=3)
    await seed(
        session_factory,
        [
            ("A", "a@example.com", EmailStatus.ACCEPTED, recent),
            ("B", "b@example.com", EmailStatus.DELIVERED, old),
            ("C", "c@example.com", EmailStatus.FAILED, recent),
            ("D", "d@example.com", EmailStatus.PENDING, old),
            ("E", "e@example.com", EmailStatus.SOFT_BOUNCED, old),
            ("F", "f@example.com", EmailStatus.RETRYING, old),
        ],
    )

    response = await admin.get(f"{ADMIN}/")

    assert response.status_code == 200
    cards = dict(
        re.findall(
            r'<div class="subheader">([^<]+)</div>\s*<div class="h1 mb-0">(\d+)</div>',
            response.text,
        )
    )
    assert cards == {
        "Total submissions": "6",
        "Delivered": "1",
        "Accepted, in progress": "2",
        "Bounced, blocked or failed": "2",
        "Pending": "1",
        "Last 24 hours": "2",
    }


async def test_list_shows_every_column_newest_first(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    rows = await seed(session_factory, THREE)

    response = await admin.get(LIST)

    assert response.status_code == 200
    html = response.text
    for row in rows:
        assert str(row.id) in html
        assert row.name in html
        assert row.email in html
    assert "2026-09-21 12:00:00" in html
    assert re.search(r">\s*failed\s*<", html)
    # Default order: created_at descending, so Carol (newest) is listed before Alice.
    assert uuids_in(html) == [str(rows[2].id), str(rows[1].id), str(rows[0].id)]


async def test_search_matches_name_and_email(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed(session_factory, THREE)

    by_name = await admin.get(LIST, params={"search": "alice ad"})
    by_email = await admin.get(LIST, params={"search": "example.org"})
    nothing = await admin.get(LIST, params={"search": "zzz"})

    assert "alice@example.com" in by_name.text
    assert "bob@example.org" not in by_name.text
    assert "bob@example.org" in by_email.text
    assert "alice@example.com" not in by_email.text
    assert uuids_in(nothing.text) == []
    assert "Alice Adams" not in nothing.text


async def test_search_finds_a_submission_by_its_uuid(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    alice, bob, _ = await seed(session_factory, THREE)

    response = await admin.get(LIST, params={"search": str(bob.id)})

    assert response.status_code == 200
    assert uuids_in(response.text) == [str(bob.id)]
    assert "bob@example.org" in response.text
    assert "alice@example.com" not in response.text
    assert str(alice.id) not in response.text


@pytest.mark.parametrize(
    "spelling",
    [
        "{id}",
        "  {id}  ",
        "{upper}",
        "{{{id}}}",  # the braced form uuid.UUID accepts
        "urn:uuid:{id}",
        "{hex}",  # 32 hex digits, no dashes
    ],
)
async def test_every_spelling_of_one_uuid_finds_the_same_row(
    admin: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    spelling: str,
) -> None:
    (row,) = await seed(session_factory, THREE[:1])
    term = spelling.format(id=row.id, upper=str(row.id).upper(), hex=row.id.hex)

    response = await admin.get(LIST, params={"search": term})

    assert uuids_in(response.text) == [str(row.id)]


@pytest.mark.parametrize(
    "term",
    [
        "0199c0e1-1234-7abc-89de",  # too short
        "not-a-uuid-at-all",
        "zzzzzzzz-zzzz-7zzz-zzzz-zzzzzzzzzzzz",  # right shape, not hexadecimal
        "0199c0e1-1234-7abc-89de-0123456789ab-extra",
        "' OR 1=1 --",
    ],
)
async def test_input_that_only_looks_like_a_uuid_finds_nothing_and_does_not_raise(
    admin: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    term: str,
) -> None:
    await seed(session_factory, THREE)

    response = await admin.get(LIST, params={"search": term})

    assert response.status_code == 200
    assert uuids_in(response.text) == []
    for name in ("Alice Adams", "Bob Brown", "Carol Clark"):
        assert name not in response.text


async def test_a_well_formed_uuid_for_no_row_finds_nothing(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed(session_factory, THREE)

    response = await admin.get(LIST, params={"search": str(uuid.uuid7())})

    assert response.status_code == 200
    assert uuids_in(response.text) == []
    for name in ("Alice Adams", "Bob Brown", "Carol Clark"):
        assert name not in response.text


async def test_uuid_search_combines_with_the_status_filter_and_paginates(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    rows = await seed(
        session_factory,
        THREE
        + [
            (
                f"Bulk {i:02d}",
                f"bulk{i:02d}@example.com",
                EmailStatus.ACCEPTED,
                NOW - timedelta(minutes=i),
            )
            for i in range(30)
        ],
    )
    bob = rows[1]

    matching = await admin.get(
        LIST, params={"search": str(bob.id), "email_status": "failed"}
    )
    excluded = await admin.get(
        LIST, params={"search": str(bob.id), "email_status": "accepted"}
    )
    page_one = await admin.get(LIST, params={"search": "bulk"})
    page_two = await admin.get(LIST, params={"search": "bulk", "page": 2})

    assert uuids_in(matching.text) == [str(bob.id)]
    assert uuids_in(excluded.text) == []
    assert "Bob Brown" not in excluded.text
    assert len(uuids_in(page_one.text)) == 25
    assert len(uuids_in(page_two.text)) == 5


async def test_the_search_box_says_the_id_can_be_searched(
    admin: httpx.AsyncClient,
) -> None:
    response = await admin.get(LIST)

    assert re.search(r'placeholder="[^"]*ID \(UUIDv7\)"', response.text)


@pytest.mark.parametrize(
    ("status", "email"),
    [
        ("accepted", "alice@example.com"),
        ("failed", "bob@example.org"),
        ("pending", "carol@example.net"),
    ],
)
async def test_status_filter_narrows_the_list(
    admin: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
    email: str,
) -> None:
    rows = await seed(session_factory, THREE)

    response = await admin.get(LIST, params={"email_status": status})

    assert response.status_code == 200
    assert email in response.text
    assert len(uuids_in(response.text)) == 1
    assert {str(r.id) for r in rows if r.email == email} == set(uuids_in(response.text))


@pytest.mark.parametrize(
    ("sort_by", "direction", "expected"),
    [
        ("created_at", "asc", ["Alice Adams", "Bob Brown", "Carol Clark"]),
        ("created_at", "desc", ["Carol Clark", "Bob Brown", "Alice Adams"]),
        ("name", "desc", ["Carol Clark", "Bob Brown", "Alice Adams"]),
        ("email", "asc", ["Alice Adams", "Bob Brown", "Carol Clark"]),
    ],
)
async def test_sorting_by_each_column(
    admin: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    sort_by: str,
    direction: str,
    expected: list[str],
) -> None:
    await seed(session_factory, THREE)

    response = await admin.get(LIST, params={"sortBy": sort_by, "sort": direction})

    names = re.findall(r"(Alice Adams|Bob Brown|Carol Clark)", response.text)
    assert list(dict.fromkeys(names)) == expected  # first occurrence of each, in order


async def test_sorting_by_status_groups_rows_and_reverses(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # SQLite orders the emulated enum by its text, MySQL by its declared order; the Docker
    # integration test asserts the MySQL order. Here: both directions agree and differ
    # from the default (created_at) order.
    await seed(session_factory, THREE)

    asc = await admin.get(LIST, params={"sortBy": "email_status", "sort": "asc"})
    desc = await admin.get(LIST, params={"sortBy": "email_status", "sort": "desc"})

    names = re.compile(r"(Alice Adams|Bob Brown|Carol Clark)")
    ascending = list(dict.fromkeys(names.findall(asc.text)))
    descending = list(dict.fromkeys(names.findall(desc.text)))
    assert len(ascending) == 3
    assert descending == ascending[::-1]
    assert ascending != ["Carol Clark", "Bob Brown", "Alice Adams"]


async def test_pagination_defaults_to_25_rows_and_offers_50_and_100(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed(
        session_factory,
        [
            (
                f"User {i:02d}",
                f"u{i:02d}@example.com",
                EmailStatus.ACCEPTED,
                NOW - timedelta(minutes=i),
            )
            for i in range(30)
        ],
    )

    first = await admin.get(LIST)
    second = await admin.get(LIST, params={"page": 2})
    all_at_once = await admin.get(LIST, params={"pageSize": 50})

    assert len(uuids_in(first.text)) == 25
    assert len(uuids_in(second.text)) == 5
    assert set(uuids_in(first.text)).isdisjoint(uuids_in(second.text))
    assert len(uuids_in(all_at_once.text)) == 30
    for option in (25, 50, 100):
        assert f"pageSize={option}" in first.text


async def test_details_page_opens_one_submission_by_uuid(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    (row,) = await seed(session_factory, THREE[:1])

    response = await admin.get(f"{ADMIN}/submission/details/{row.id}")

    assert response.status_code == 200
    assert str(row.id) in response.text
    assert "alice@example.com" in response.text
    assert "Alice Adams" in response.text


async def test_details_show_the_mailjet_message_id_and_the_list_does_not(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    (row,) = await seed(session_factory, THREE[:1])
    async with session_factory() as session:
        stored = await session.get(Submission, row.id)
        assert stored is not None
        stored.mailjet_message_id = 288230376151711744
        await session.commit()

    details = await admin.get(f"{ADMIN}/submission/details/{row.id}")
    listing = await admin.get(LIST)

    assert "Mailjet MessageID" in details.text
    assert "288230376151711744" in details.text
    assert "288230376151711744" not in listing.text


@pytest.mark.parametrize("key", [str(uuid.uuid7()), "not-a-uuid", "42"])
async def test_unknown_or_malformed_ids_are_not_found(
    admin: httpx.AsyncClient, key: str
) -> None:
    response = await admin.get(f"{ADMIN}/submission/details/{key}")

    assert response.status_code == 404


# --- read-only -------------------------------------------------------------------------


async def test_list_and_details_offer_no_mutating_controls(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    (row,) = await seed(session_factory, THREE[:1])

    listing = (await admin.get(LIST)).text
    details = (await admin.get(f"{ADMIN}/submission/details/{row.id}")).text

    for html in (listing, details):
        assert "/submission/create" not in html
        assert "/submission/edit/" not in html
        assert "/submission/export/" not in html
        assert "/submission/import" not in html
        assert 'name="pks"' not in html  # the bulk-delete checkbox column
        assert "modal-delete" not in html


async def test_mutating_routes_are_refused_and_change_nothing(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    (row,) = await seed(session_factory, THREE[:1])
    base = f"{ADMIN}/submission"

    attempts = [
        await admin.get(f"{base}/create"),
        await admin.post(
            f"{base}/create", data={"name": "X", "email": "x@example.com"}
        ),
        await admin.get(f"{base}/edit/{row.id}"),
        await admin.post(f"{base}/edit/{row.id}", data={"name": "Changed"}),
        await admin.delete(f"{base}/delete", params={"pks": str(row.id)}),
        await admin.get(f"{base}/export/csv"),
        await admin.get(f"{base}/export/json"),
        await admin.post(
            f"{base}/import", files={"file": ("rows.csv", b"name,email\n")}
        ),
    ]

    assert [r.status_code for r in attempts] == [403] * len(attempts)
    assert await count_submissions(session_factory) == 1
    async with session_factory() as session:
        stored = await session.get(Submission, row.id)
    assert stored is not None
    assert stored.name == "Alice Adams"


async def test_admin_is_not_part_of_the_public_api_document(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    assert not any(path.startswith(ADMIN) for path in paths)
    assert "admin" not in str(paths).lower()
