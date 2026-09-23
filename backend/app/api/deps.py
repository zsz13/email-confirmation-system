"""FastAPI dependencies handing out the shared resources kept on ``app.state``.

The session factory is created in ``create_app``; the email service in the lifespan.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.email import EmailService
from app.services.rate_limit import ClientRateLimiter, SubmissionRateLimiter


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one AsyncSession per request; closing it rolls back anything uncommitted."""
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    async with session_factory() as session:
        yield session


def get_email_service(request: Request) -> EmailService:
    return request.app.state.email_service


def get_rate_limiter(request: Request) -> SubmissionRateLimiter:
    return request.app.state.rate_limiter


def get_status_rate_limiter(request: Request) -> ClientRateLimiter:
    return request.app.state.status_rate_limiter


def get_client_ip(request: Request) -> str | None:
    """The peer address as seen by the server, or by the trusted reverse proxy in front of it.

    ``None`` when the ASGI server did not supply one (for example over a Unix socket).
    """
    return request.client.host if request.client else None


SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmailServiceDep = Annotated[EmailService, Depends(get_email_service)]
RateLimiterDep = Annotated[SubmissionRateLimiter, Depends(get_rate_limiter)]
StatusRateLimiterDep = Annotated[ClientRateLimiter, Depends(get_status_rate_limiter)]
ClientIpDep = Annotated[str | None, Depends(get_client_ip)]
