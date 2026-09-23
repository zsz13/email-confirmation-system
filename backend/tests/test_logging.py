"""Logging configuration, request correlation and what must never reach a log line."""

import logging
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from app.api.deps import get_email_service, get_session
from app.core.config import (
    DEMO_ADMIN_PASSWORD,
    DEMO_ADMIN_PATH,
    DEMO_ADMIN_SESSION_SECRET,
    DEMO_ADMIN_USERNAME,
    Settings,
)
from app.core.logging import (
    HANDLER_NAME,
    LOG_FORMAT,
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    RequestIdFilter,
    configure_logging,
    mask_email,
    new_request_id,
    request_id_var,
    resolve_request_id,
    safe_path,
)
from app.main import create_app, lifespan
from tests.conftest import RECIPIENT, VALID_PAYLOAD, FakeEmailService

GENERATED_ID_LENGTH = len(new_request_id())


def record(message: str = "hello", name: str = "app.test") -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, message, None, None)


def request_id_of(entry: logging.LogRecord) -> str:
    """``request_id`` is put on the record by the filter, so the type checker cannot see it."""
    return getattr(entry, "request_id", "")


def app_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only what this application logged; third-party libraries have their own loggers."""
    return [r for r in caplog.records if r.name.startswith("app.")]


def completed(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """The one 'request completed' line of the request the test just made."""
    lines = [
        r for r in app_records(caplog) if r.getMessage().startswith("request completed")
    ]
    assert len(lines) == 1, [r.getMessage() for r in app_records(caplog)]
    return lines[0]


# --- configuration ---------------------------------------------------------------------


def test_configure_logging_installs_one_handler_and_levels_only_this_application() -> (
    None
):
    root = logging.getLogger()
    app_logger = logging.getLogger("app")
    previous = (root.level, app_logger.level)
    try:
        configure_logging("WARNING")
        configure_logging("DEBUG")  # a second application must not add a second handler

        handlers = [h for h in root.handlers if h.get_name() == HANDLER_NAME]
        assert len(handlers) == 1
        assert app_logger.level == logging.DEBUG
        # Third-party loggers keep WARNING, so DEBUG cannot start printing bound SQL values.
        assert root.level == logging.WARNING
        formatter = handlers[0].formatter
        assert formatter is not None
        assert formatter._fmt == LOG_FORMAT
    finally:
        root.setLevel(previous[0])
        app_logger.setLevel(previous[1])


def test_log_lines_carry_the_id_of_the_request_being_handled() -> None:
    entry = record()
    token = request_id_var.set("req-1")
    try:
        assert RequestIdFilter().filter(entry) is True
    finally:
        request_id_var.reset(token)

    assert "[req-1] app.test: hello" in logging.Formatter(LOG_FORMAT).format(entry)


def test_lines_logged_outside_a_request_still_format() -> None:
    entry = record("application startup")
    RequestIdFilter().filter(entry)

    assert request_id_of(entry) == "-"
    assert "[-] app.test: application startup" in logging.Formatter(LOG_FORMAT).format(
        entry
    )


# --- request ids -----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["abc-123", "A.b_C-9", "x" * 64])
def test_a_safe_client_request_id_is_kept(value: str) -> None:
    assert resolve_request_id(value) == value


@pytest.mark.parametrize(
    "value", [None, "", " ", "a b", "x" * 65, "id;injected", "../etc/passwd", "ünïcode"]
)
def test_anything_else_gets_a_fresh_request_id(value: str | None) -> None:
    generated = resolve_request_id(value)

    assert generated != value
    assert len(generated) == GENERATED_ID_LENGTH
    assert generated.isalnum()


async def test_the_response_carries_the_clients_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/health", headers={REQUEST_ID_HEADER: "from-the-client"}
    )

    assert response.headers[REQUEST_ID_HEADER] == "from-the-client"


async def test_an_unsafe_request_id_is_replaced_not_echoed(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/health", headers={REQUEST_ID_HEADER: "spoofed value"}
    )

    assert response.headers[REQUEST_ID_HEADER] != "spoofed value"
    assert len(response.headers[REQUEST_ID_HEADER]) == GENERATED_ID_LENGTH


async def test_a_request_without_an_id_is_given_one(client: httpx.AsyncClient) -> None:
    first = await client.get("/api/v1/health")
    second = await client.get("/api/v1/health")

    assert len(first.headers[REQUEST_ID_HEADER]) == GENERATED_ID_LENGTH
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_non_http_connections_pass_straight_through() -> None:
    seen: list[str] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        raise AssertionError("the middleware must not touch a non-HTTP connection")

    async def send(message: Message) -> None:
        raise AssertionError("the middleware must not touch a non-HTTP connection")

    await RequestContextMiddleware(downstream)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


# --- what gets logged ------------------------------------------------------------------


async def test_a_completed_request_is_logged_with_its_context(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    line = completed(caplog)
    assert line.levelno == logging.INFO
    assert request_id_of(line) == response.headers[REQUEST_ID_HEADER]
    assert "method=POST" in line.getMessage()
    assert "path='/api/v1/submissions'" in line.getMessage()  # quoted: see safe_path
    assert "status_code=201" in line.getMessage()
    duration = re.search(r"duration_ms=([\d.]+)", line.getMessage())
    assert duration is not None
    assert (
        0 < float(duration.group(1)) < 60_000
    )  # milliseconds, not seconds and not a constant


async def test_health_checks_are_logged_at_debug_only(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The container polls /api/v1/health every ten seconds; at INFO it would bury the rest.
    with caplog.at_level(logging.INFO, logger="app"):
        await client.get("/api/v1/health")
    assert app_records(caplog) == []

    with caplog.at_level(logging.DEBUG, logger="app"):
        await client.get("/api/v1/health")
    assert completed(caplog).levelno == logging.DEBUG


async def test_a_failing_request_is_logged_with_its_status_and_still_correlated(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    class ExplodingEmailService:
        async def send_confirmation(
            self, *, recipient_name: str, recipient_email: str
        ) -> None:
            raise RuntimeError("boom")

    app.dependency_overrides[get_email_service] = ExplodingEmailService

    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 500
    assert "status_code=500" in completed(caplog).getMessage()
    assert response.headers[REQUEST_ID_HEADER] == request_id_of(completed(caplog))
    assert any(
        r.levelno == logging.ERROR and "unhandled application error" in r.getMessage()
        for r in app_records(caplog)
    )


async def test_the_submission_lifecycle_is_logged(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    messages = [r.getMessage() for r in app_records(caplog)]
    assert any(m.startswith("submission created submission_id=") for m in messages)
    assert any(
        m.startswith("delivery_status_updated ") and "new_status=accepted" in m
        for m in messages
    )
    # One request, one id: every line of it can be grepped out of the container log.
    assert len({request_id_of(r) for r in app_records(caplog)}) == 1


async def test_a_failed_delivery_is_logged_as_an_error(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_email_service.fail = True

    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 502
    assert any(
        r.levelno == logging.ERROR
        and "email delivery failed" in r.getMessage()
        and "email_status=failed" in r.getMessage()
        for r in app_records(caplog)
    )


async def test_a_rate_limited_request_is_logged_as_a_warning(
    client: httpx.AsyncClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    for _ in range(settings.rate_limit_email_max_requests):
        await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 429
    assert any(
        r.levelno == logging.WARNING and "rate limit rejected" in r.getMessage()
        for r in app_records(caplog)
    )


# --- what must never be logged ----------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("daniyar@example.com", "d***@example.com"),
        ("a@example.com", "a***@example.com"),
        ("first.last+tag@mailinator.com", "f***@mailinator.com"),
        ("not-an-address", "***"),
        ("", "***"),
    ],
)
def test_recipients_are_masked(address: str, masked: str) -> None:
    assert mask_email(address) == masked


async def test_no_secret_and_no_full_address_reaches_the_log(
    client: httpx.AsyncClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """At DEBUG, the noisiest level there is, and with secrets in the request headers.

    Credentials must appear nowhere at all. The recipient address is checked against what
    the application itself logs: at DEBUG the database driver traces every statement with
    its bound parameters, which is the diagnostic output an operator asked for by choosing
    that level, and is the reason the default is INFO.
    """
    with caplog.at_level(logging.DEBUG, logger="app"):
        # A successful admin login: the one request that carries the password itself.
        await client.post(
            f"{DEMO_ADMIN_PATH}/login",
            data={"username": DEMO_ADMIN_USERNAME, "password": DEMO_ADMIN_PASSWORD},
        )
        await client.post("/api/v1/submissions", json=VALID_PAYLOAD)
        await client.post(
            "/api/v1/submissions",
            json=VALID_PAYLOAD,
            headers={"Authorization": "Bearer a-token", "Cookie": "session=a-cookie"},
        )

    application_log = "\n".join(r.getMessage() for r in app_records(caplog))
    assert RECIPIENT not in application_log
    assert mask_email(RECIPIENT) in application_log

    forbidden = [
        settings.database_url,
        settings.mailjet_api_key.get_secret_value(),
        settings.mailjet_secret_key.get_secret_value(),
        settings.admin_password.get_secret_value(),
        settings.admin_session_secret.get_secret_value(),
        DEMO_ADMIN_PASSWORD,
        DEMO_ADMIN_SESSION_SECRET,
        "Bearer a-token",
        "session=a-cookie",
    ]
    assert app_records(caplog)  # the request really was logged
    for secret in forbidden:
        assert secret not in caplog.text


# --- a client cannot write its own log lines ---------------------------------------------


async def test_a_client_cannot_forge_a_log_line_through_the_path(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The server percent-decodes the target, so a path can arrive holding a newline."""
    forged = (
        "/api/v1/x%0A2026-01-01 CRITICAL [forged] app.admin.auth: admin login succeeded"
    )

    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.get(forged)

    assert response.status_code == 404
    message = completed(caplog).getMessage()
    assert "\n" not in message
    assert "\\n" in message  # the newline is escaped, not executed
    assert "CRITICAL" not in caplog.text.split("request completed")[0]


async def test_a_logged_path_is_bounded(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        await client.get("/api/v1/" + "a" * 5000)

    assert len(completed(caplog).getMessage()) < 500


async def test_a_request_the_application_never_answers_is_logged_as_a_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exploding(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("nothing was sent")

    async def receive() -> Message:
        raise AssertionError("not reached")

    async def send(message: Message) -> None:
        raise AssertionError("not reached")

    scope: Scope = {"type": "http", "method": "GET", "path": "/boom", "headers": []}

    with (
        caplog.at_level(logging.INFO, logger="app"),
        pytest.raises(RuntimeError),
    ):
        await RequestContextMiddleware(exploding)(scope, receive, send)

    assert "status_code=500" in completed(caplog).getMessage()
    assert request_id_var.get() == "-"  # the context is restored for the next request


# --- levels ------------------------------------------------------------------------------


def test_the_level_defaults_to_info_and_comes_from_the_settings(
    settings: Settings,
) -> None:
    assert Settings.model_fields["log_level"].default == "INFO"
    previous = logging.getLogger("app").level
    try:
        create_app(settings.model_copy(update={"log_level": "WARNING"}))
        assert logging.getLogger("app").level == logging.WARNING

        create_app(settings.model_copy(update={"log_level": "DEBUG"}))
        assert logging.getLogger("app").level == logging.DEBUG
    finally:
        logging.getLogger("app").setLevel(previous)


async def test_debug_does_not_turn_on_the_database_driver(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """LOG_LEVEL is the application's level: the driver traces statements with their values."""
    with caplog.at_level(logging.DEBUG, logger="app"):
        await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert app_records(caplog)
    assert logging.getLogger().level == logging.WARNING
    assert not [r for r in caplog.records if r.name.startswith("aiosqlite")]


# --- the events the application must report ----------------------------------------------


async def test_startup_and_shutdown_are_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings)

    with caplog.at_level(logging.INFO, logger="app"):
        async with lifespan(app):
            pass

    messages = [r.getMessage() for r in app_records(caplog)]
    assert any(m.startswith("application startup") for m in messages)
    assert any(m.startswith("application shutdown") for m in messages)


async def test_startup_says_the_admin_panel_is_on_without_saying_where(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings)

    with caplog.at_level(logging.DEBUG, logger="app"):
        async with lifespan(app):
            pass

    startup = next(
        r.getMessage()
        for r in app_records(caplog)
        if r.getMessage().startswith("application startup")
    )
    assert "admin_enabled=true" in startup
    assert settings.admin_path not in caplog.text
    assert DEMO_ADMIN_PATH.strip("/") not in caplog.text


async def test_admin_requests_are_logged_without_the_admin_path(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        await client.get(f"{DEMO_ADMIN_PATH}/login")
        await client.get(DEMO_ADMIN_PATH)

    lines = [
        r.getMessage()
        for r in app_records(caplog)
        if r.getMessage().startswith("request completed")
    ]
    assert "path='<admin>/login'" in lines[0]
    assert "path='<admin>'" in lines[1]
    assert DEMO_ADMIN_PATH.strip("/") not in caplog.text


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/ops", "'<admin>'"),
        ("/ops/submission/list", "'<admin>/submission/list'"),
        # Only the mount point itself, not a path that merely starts with the same text.
        ("/opsx/list", "'/opsx/list'"),
        ("/api/v1/submissions", "'/api/v1/submissions'"),
    ],
)
def test_only_the_admin_mount_point_is_hidden(path: str, logged: str) -> None:
    assert safe_path(path, "/ops") == logged


async def test_a_submission_logs_where_its_time_went(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    timings = next(
        r
        for r in app_records(caplog)
        if r.getMessage().startswith("submission timings")
    )
    message = timings.getMessage()
    assert request_id_of(timings) == response.headers[REQUEST_ID_HEADER]
    assert re.search(r"submission_id=[0-9a-f-]{36} ", message)
    values = {
        name: float(value)
        for name, value in re.findall(
            r"(\w+_duration_ms|duration_ms)=([\d.]+)", message
        )
    }
    assert set(values) == {
        "db_create_duration_ms",
        "mailjet_request_duration_ms",
        "db_status_update_duration_ms",
        "duration_ms",
    }
    stages = sum(v for k, v in values.items() if k != "duration_ms")
    # Each value is rounded to 0.1 ms on its own, so the logged stages may add up to a
    # little more than the logged total: at most half a unit for each of the four values.
    rounding = 0.05 * len(values)
    assert stages <= values["duration_ms"] + rounding
    assert values["duration_ms"] < 60_000
    # Internal only: nothing about timing reaches the client.
    assert "duration" not in response.text
    assert not any("duration" in header.lower() for header in response.headers)


async def test_a_failed_send_still_logs_its_timings(
    client: httpx.AsyncClient,
    fake_email_service: FakeEmailService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_email_service.fail = True

    with caplog.at_level(logging.INFO, logger="app"):
        await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    message = next(
        r.getMessage()
        for r in app_records(caplog)
        if r.getMessage().startswith("submission timings")
    )
    assert "mailjet_request_duration_ms=" in message
    assert "db_status_update_duration_ms=" in message


async def test_a_database_failure_is_logged_as_an_error_with_its_context(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    async def unavailable_session() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("mysql is down"))

    app.dependency_overrides[get_session] = unavailable_session

    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post("/api/v1/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 503
    failure = [
        r
        for r in app_records(caplog)
        if r.levelno == logging.ERROR and "database failure" in r.getMessage()
    ]
    assert len(failure) == 1
    assert "path='/api/v1/submissions'" in failure[0].getMessage()
    assert failure[0].exc_info is not None  # the traceback is kept for diagnosis


async def test_a_database_error_does_not_carry_the_submitted_values(
    app: FastAPI,
) -> None:
    """SQLAlchemy puts bound parameters in the exception text, which the handler logs."""
    async with app.state.session_factory() as session:
        with pytest.raises(SQLAlchemyError) as raised:
            await session.execute(
                text("INSERT INTO nowhere (email) VALUES (:email)"),
                {"email": RECIPIENT},
            )

    assert RECIPIENT not in str(raised.value)


async def test_a_failed_admin_login_is_logged_as_a_warning(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        response = await client.post(
            f"{DEMO_ADMIN_PATH}/login",
            data={"username": DEMO_ADMIN_USERNAME, "password": "wrong-password"},
        )

    assert response.status_code == 400
    assert any(
        r.levelno == logging.WARNING and "admin login failed" in r.getMessage()
        for r in app_records(caplog)
    )
    assert "wrong-password" not in caplog.text
