"""Logging configuration and request correlation.

Everything the application logs goes to stdout in one format, so `docker compose logs
backend` is the only tool needed to read it. Each line carries the id of the request it
belongs to, which is also returned to the client in the ``X-Request-ID`` header.

Nothing here writes a credential: secrets live in ``SecretStr`` fields, the database URL is
never logged, and recipient addresses are masked with :func:`mask_email` before they reach a
log record.
"""

import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
HANDLER_NAME = "app-stdout"

REQUEST_ID_HEADER = "X-Request-ID"
# What a client may echo back to us: the shape of an id this service would generate itself,
# plus the common hyphenated and dotted forms. Anything else (control characters, header
# injection, an unbounded string used to flood the log) is replaced by a fresh id.
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# The container health check polls this every ten seconds; at INFO it would bury everything.
HEALTH_PATH = "/api/v1/health"
# How much of a client-controlled path one log line may hold.
MAX_LOGGED_PATH = 200
# What a logged path shows in place of the admin panel's mount point.
ADMIN_PATH_PLACEHOLDER = "<admin>"

# The id of the request being handled, or "-" outside a request (startup, shutdown, tests).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger("app.request")


def new_request_id() -> str:
    return uuid.uuid4().hex


def resolve_request_id(value: str | None) -> str:
    """The client's request id when it is safe to log and echo, otherwise a fresh one."""
    if value is not None and SAFE_REQUEST_ID.fullmatch(value):
        return value
    return new_request_id()


def safe_path(path: str, admin_path: str | None = None) -> str:
    """Quote a client-controlled path for a log line, with the admin mount point hidden.

    Uvicorn percent-decodes the request target before the application sees it, so ``path``
    can hold newlines and control characters. Written out as it stands, a client could forge
    whole log records by asking for ``/x%0A2026-01-01 CRITICAL admin login succeeded``.
    ``repr`` escapes them, and the cap bounds how much of the log one request can take.

    The admin panel's path is replaced by :data:`ADMIN_PATH_PLACEHOLDER`: logs travel further
    than the people meant to know where the panel is.
    """
    if admin_path and (path == admin_path or path.startswith(f"{admin_path}/")):
        path = ADMIN_PATH_PLACEHOLDER + path[len(admin_path) :]
    return repr(path[:MAX_LOGGED_PATH])


def mask_email(email: str) -> str:
    """``daniyar@example.com`` -> ``d***@example.com``.

    Enough to recognise an address you already know while reading a log, not enough to
    harvest one from it.
    """
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


class RequestIdFilter(logging.Filter):
    """Puts ``request_id`` on every record, so :data:`LOG_FORMAT` can always print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: str) -> None:
    """Send this application's loggers to stdout in :data:`LOG_FORMAT`.

    ``LOG_LEVEL`` sets the level of the ``app.*`` loggers only. Everything else keeps
    ``WARNING``, because a third-party logger at DEBUG undoes the promise that no submitted
    address is ever logged in full: the database driver traces every statement with its bound
    parameters. Warnings and errors from libraries still come through.

    Uvicorn configures only its own loggers, so without this the ``app.*`` loggers would have
    no handler at all. Called once per application; a second call only adjusts the level, and
    never removes a handler installed by something else (pytest's capture, for example).
    """
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    logging.getLogger("app").setLevel(level)
    if any(handler.get_name() == HANDLER_NAME for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)


class RequestContextMiddleware:
    """Give every request an id, return it in ``X-Request-ID`` and log how the request ended.

    Written against the ASGI interface rather than Starlette's ``BaseHTTPMiddleware``: it
    needs no access to the body, adds no task per request, and the context variable it sets
    stays visible to every handler and log call downstream.
    """

    def __init__(self, app: ASGIApp, admin_path: str | None = None) -> None:
        self.app = app
        self.admin_path = admin_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        # Only overwritten once the response starts; an exception that escapes the
        # application is what the server turns into a 500.
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.log(
                logging.DEBUG if scope["path"] == HEALTH_PATH else logging.INFO,
                "request completed method=%s path=%s status_code=%d duration_ms=%.1f",
                scope["method"],
                safe_path(scope["path"], self.admin_path),
                status_code,
                duration_ms,
            )
            request_id_var.reset(token)
