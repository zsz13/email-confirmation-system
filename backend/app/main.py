"""ASGI application factory and entrypoint (``uvicorn app.main:app``)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.admin import mount_admin
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    configure_logging,
    safe_path,
)
from app.schemas.error import ErrorResponse
from app.services.delivery_reconciler import DeliveryReconciler
from app.services.delivery_status import MailjetStatusClient
from app.services.email import EmailService
from app.services.rate_limit import (
    ClientRateLimiter,
    RateLimit,
    SubmissionRateLimiter,
)

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_DETAIL = (
    "The database is currently unavailable. Please try again later."
)
UNEXPECTED_ERROR_DETAIL = (
    "Something went wrong while processing the request. Please try again."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold the shared HTTP client and the delivery reconciler for the process lifetime.

    The reconciler is stopped before the HTTP client closes and the database pool is
    released, so no lookup is left running against either.
    """
    settings: Settings = app.state.settings
    async with httpx.AsyncClient(timeout=settings.mailjet_timeout_seconds) as client:
        app.state.email_service = EmailService(client=client, settings=settings)
        reconciler = DeliveryReconciler(
            app.state.session_factory,
            MailjetStatusClient(client, settings),
            interval_seconds=settings.delivery_reconcile_interval_seconds,
        )
        app.state.delivery_reconciler = reconciler
        logger.info(
            # The panel's path is deliberately not logged; see safe_path.
            "application startup app=%s log_level=%s admin_enabled=true",
            settings.app_name,
            settings.log_level,
        )
        reconciler.start()
        try:
            yield
        finally:
            await reconciler.stop()
            logger.info("application shutdown")
    await app.state.engine.dispose()


async def handle_database_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "database failure method=%s path=%s",
        request.method,
        safe_path(request.url.path, request.app.state.settings.admin_path),
        exc_info=exc,
    )
    return JSONResponse(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        content=ErrorResponse(detail=DATABASE_UNAVAILABLE_DETAIL).model_dump(),
    )


async def catch_unexpected_errors(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    """Last-resort boundary so unexpected failures still answer with JSON and CORS headers.

    Starlette's built-in 500 handler runs outside the middleware stack, so its response has
    no CORS headers and the browser would report the API as unreachable rather than failed.
    """
    try:
        return await call_next(request)
    except Exception:  # deliberate catch-all at the process boundary
        logger.exception(
            "unhandled application error method=%s path=%s",
            request.method,
            safe_path(request.url.path, request.app.state.settings.admin_path),
        )
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content=ErrorResponse(detail=UNEXPECTED_ERROR_DETAIL).model_dump(),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Stores a name and email address, then sends a confirmation email through Mailjet. "
            "All endpoints live under `/api/v1`."
        ),
        version="1.0.0",
        lifespan=lifespan,
        # No 307 to a URL built from the client-supplied Host header; a trailing slash is 404.
        redirect_slashes=False,
    )
    app.state.settings = settings
    if settings.admin_uses_demo_defaults:
        logger.warning(
            "Admin panel is using the committed demo credentials; override ADMIN_PASSWORD "
            "and ADMIN_SESSION_SECRET before exposing this service"
        )
    # Resource ownership: the engine and session factory are created here, because the
    # admin panel needs them while the application is being assembled, and disposed in the
    # lifespan; the HTTP client (and with it the email service and the delivery reconciler)
    # lives in the lifespan only.
    # The engine opens no connection until first use. pool_pre_ping transparently replaces
    # connections MySQL closed after wait_timeout; expire_on_commit=False keeps committed
    # attributes readable without an implicit reload, which would fail in async code.
    # hide_parameters keeps the submitted name and address out of SQLAlchemy's exception
    # text, which the database-failure handler logs with its traceback.
    engine = create_async_engine(
        settings.database_url, pool_pre_ping=True, hide_parameters=True
    )
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.rate_limiter = SubmissionRateLimiter(
        per_ip=RateLimit(
            settings.rate_limit_ip_max_requests, settings.rate_limit_ip_window_seconds
        ),
        per_email=RateLimit(
            settings.rate_limit_email_max_requests,
            settings.rate_limit_email_window_seconds,
        ),
    )
    app.state.status_rate_limiter = ClientRateLimiter(
        RateLimit(
            settings.rate_limit_status_ip_max_requests,
            settings.rate_limit_status_ip_window_seconds,
        )
    )

    # Middleware added later wraps middleware added earlier, so the request context ends up
    # outermost: every response carries a request id and every request is logged once, and
    # CORS decorates everything inside it, including the generic 500 produced one layer in.
    app.add_middleware(BaseHTTPMiddleware, dispatch=catch_unexpected_errors)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", REQUEST_ID_HEADER],
        # Lets the browser read the wait time on a 429, and the request id of any response,
        # during cross-origin development.
        expose_headers=["Retry-After", REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestContextMiddleware, admin_path=settings.admin_path)
    app.add_exception_handler(SQLAlchemyError, handle_database_error)
    app.include_router(api_router)
    # Mounted, not routed: the panel is served by the same process but is not part of the
    # public API, so it does not appear in the OpenAPI document.
    mount_admin(app, engine=engine, settings=settings)
    return app


app = create_app()
