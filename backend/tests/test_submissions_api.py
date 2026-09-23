import logging
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_email_service, get_session
from app.api.v1.routes.submissions import EMAIL_ACCEPTED_DETAIL, EMAIL_FAILED_DETAIL
from app.main import DATABASE_UNAVAILABLE_DETAIL, UNEXPECTED_ERROR_DETAIL
from app.models.submission import EmailStatus, Submission
from tests.conftest import (
    FIRST_MESSAGE_ID,
    RECIPIENT,
    VALID_PAYLOAD,
    FakeEmailService,
    count_submissions,
)


async def test_valid_submission_is_stored_and_confirmation_sent(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 201
    # The public contract is the message and the tracking id, nothing else: no echo of
    # the input, no status, no timestamps, nothing about the email provider.
    body = response.json()
    assert body == {"detail": EMAIL_ACCEPTED_DETAIL, "tracking_id": body["tracking_id"]}

    assert fake_email_service.sent == [("Daniyar", RECIPIENT)]

    async with session_factory() as session:
        stored = (await session.execute(select(Submission))).scalar_one()
    assert stored.name == "Daniyar"
    assert stored.email == RECIPIENT
    assert stored.email_status is EmailStatus.ACCEPTED
    assert stored.created_at.tzinfo is not None
    assert isinstance(stored.id, uuid.UUID)
    assert stored.id.version == 7
    # The tracking id is the row's UUIDv7, in canonical form.
    assert body["tracking_id"] == str(stored.id)


async def test_name_is_trimmed_before_storage_and_sending(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post(
        "/api/v1/submissions", json={"name": "  Daniyar  ", "email": RECIPIENT}
    )

    assert response.status_code == 201
    assert fake_email_service.sent == [("Daniyar", RECIPIENT)]
    async with session_factory() as session:
        stored = (await session.execute(select(Submission))).scalar_one()
    assert stored.name == "Daniyar"


@pytest.mark.parametrize(
    ("payload", "invalid_field"),
    [
        pytest.param({"email": RECIPIENT}, "name", id="missing-name"),
        pytest.param({"name": "   ", "email": RECIPIENT}, "name", id="blank-name"),
        pytest.param(
            {"name": "x" * 101, "email": RECIPIENT}, "name", id="name-too-long"
        ),
        pytest.param(
            {"name": "Dan\niyar", "email": RECIPIENT}, "name", id="control-characters"
        ),
        pytest.param(
            {"name": "Daniyar", "email": f"{'x' * 250}@example.com"},
            "email",
            id="email-too-long",
        ),
        pytest.param({"name": "Daniyar"}, "email", id="missing-email"),
        pytest.param(
            {"name": "Daniyar", "email": "not-an-email"}, "email", id="invalid-email"
        ),
        pytest.param(
            {"name": "Daniyar", "email": "missing-domain@"}, "email", id="no-domain"
        ),
    ],
)
async def test_invalid_payload_is_rejected_without_side_effects(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, str],
    invalid_field: str,
) -> None:
    response = await client.post("/api/v1/submissions", json=payload)

    assert response.status_code == 422
    invalid_locations = {error["loc"][-1] for error in response.json()["detail"]}
    assert invalid_field in invalid_locations
    assert fake_email_service.sent == []
    assert await count_submissions(session_factory) == 0


async def test_name_at_maximum_length_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/submissions", json={"name": "x" * 100, "email": RECIPIENT}
    )

    assert response.status_code == 201


async def test_email_failure_marks_submission_failed_and_returns_502(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_email_service.fail = True

    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 502
    assert response.json() == {"detail": EMAIL_FAILED_DETAIL}

    async with session_factory() as session:
        result = await session.execute(select(Submission))
        stored = result.scalar_one()
    assert stored.email == RECIPIENT
    assert stored.email_status is EmailStatus.FAILED
    assert stored.mailjet_message_id is None


async def test_send_api_acceptance_is_stored_as_accepted_not_delivered(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 201
    async with session_factory() as session:
        stored = (await session.execute(select(Submission))).scalar_one()
    # Mailjet has the message; whether the recipient's server took it is not known yet.
    assert stored.email_status is EmailStatus.ACCEPTED
    assert stored.mailjet_message_id == FIRST_MESSAGE_ID


async def test_the_mailjet_message_id_stays_internal(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert set(response.json()) == {"detail", "tracking_id"}
    assert str(FIRST_MESSAGE_ID) not in response.text
    assert not any(
        str(FIRST_MESSAGE_ID) in value for value in response.headers.values()
    )
    # The wording says "accepted"; the internal status field itself is never returned.
    assert "status" not in response.text


async def test_database_failure_returns_503_without_internal_details(
    app: FastAPI, client: httpx.AsyncClient, fake_email_service: FakeEmailService
) -> None:
    async def unavailable_session() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("mysql is down"))

    app.dependency_overrides[get_session] = unavailable_session

    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert "mysql is down" not in response.text
    assert fake_email_service.sent == []


async def test_unexpected_error_returns_generic_500_json(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    class ExplodingEmailService:
        async def send_confirmation(
            self, *, recipient_name: str, recipient_email: str
        ) -> None:
            raise RuntimeError(f"internal detail about {recipient_email}")

    app.dependency_overrides[get_email_service] = ExplodingEmailService
    response = await client.post(
        "/api/v1/submissions",
        json=VALID_PAYLOAD,
        headers={"Origin": "http://localhost:4200"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": UNEXPECTED_ERROR_DETAIL}
    assert "internal detail" not in response.text
    # CORS headers must survive so the browser can read the error instead of reporting "unreachable".
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"


async def test_commit_failure_after_sending_returns_503_and_leaves_row_pending(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def session_whose_second_commit_fails() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            real_commit = session.commit
            commits = 0

            async def commit() -> None:
                nonlocal commits
                commits += 1
                if commits == 2:
                    raise OperationalError(
                        "UPDATE submissions", {}, ConnectionResetError()
                    )
                await real_commit()

            monkeypatch.setattr(session, "commit", commit)
            yield session

    app.dependency_overrides[get_session] = session_whose_second_commit_fails
    response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert fake_email_service.sent == [("Daniyar", RECIPIENT)]
    async with session_factory() as session:
        stored = (await session.execute(select(Submission))).scalar_one()
    assert stored.email_status is EmailStatus.PENDING


async def test_a_status_update_that_fails_inside_the_flush_is_still_reported(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real flush failure rolls the transaction back and expires the row's attributes.

    The reconciliation warning and the timings line must still name the submission.
    """

    def fail_the_status_update(
        _connection: object, _cursor: object, statement: str, *_: object
    ) -> None:
        if statement.startswith("UPDATE submissions"):
            raise OperationalError(statement, {}, TimeoutError())

    event.listen(
        app.state.engine.sync_engine, "before_cursor_execute", fail_the_status_update
    )
    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 503
    async with session_factory() as session:
        stored = (await session.execute(select(Submission))).scalar_one()
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("app.")]
    assert (
        "confirmation email accepted but status could not be updated "
        f"submission_id={stored.id}"
    ) in messages
    assert any(
        m.startswith(f"submission timings submission_id={stored.id} ") for m in messages
    )
