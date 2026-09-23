"""Shared fixtures.

The suite never contacts MySQL or Mailjet: the application is built on a throw-away SQLite
file per test and the email service is swapped out through a FastAPI dependency override.
The rate limiter is the real one with the default limits, so a single test may make at most
five accepted POSTs from one client address (the ``client`` fixture) before it sees a 429.
"""

import os

# Importing ``app.main`` builds the ASGI app from settings. These placeholders make that
# import independent of a developer's backend/.env; the tests themselves use the explicit
# ``settings`` fixture below.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("MAILJET_API_KEY", "test-api-key")
os.environ.setdefault("MAILJET_SECRET_KEY", "test-secret-key")
os.environ.setdefault("MAILJET_SENDER_EMAIL", "sender@example.com")

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_email_service
from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.submission import Submission
from app.services.email import EmailDeliveryError

RECIPIENT = "daniyar-swe-demo-0923@mailinator.com"
VALID_PAYLOAD = {"name": "Daniyar", "email": RECIPIENT}


async def count_submissions(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(Submission))
        return result.scalar_one()


FIRST_MESSAGE_ID = 288230376151711744  # the size of a real Mailjet MessageID


class FakeEmailService:
    """Stands in for ``EmailService``: records recipients instead of calling Mailjet."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send_confirmation(
        self, *, recipient_name: str, recipient_email: str
    ) -> int:
        if self.fail:
            raise EmailDeliveryError("simulated Mailjet failure")
        self.sent.append((recipient_name, recipient_email))
        return FIRST_MESSAGE_ID + len(self.sent) - 1


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        # A BaseSettings init option Pyright cannot see; None keeps backend/.env out of tests.
        _env_file=None,  # pyright: ignore[reportCallIssue]
        # A file-backed database gives every session its own connection, so concurrent
        # requests in the tests interleave the way they do against MySQL.
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        mailjet_api_key=SecretStr("test-api-key"),
        mailjet_secret_key=SecretStr("test-secret-key"),
        mailjet_sender_email="sender@example.com",
        mailjet_sender_name="Email Confirmation System",
        cors_origins=["http://localhost:4200"],
    )


@pytest.fixture
def fake_email_service() -> FakeEmailService:
    return FakeEmailService()


@pytest.fixture
async def app(
    settings: Settings, fake_email_service: FakeEmailService
) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    application.dependency_overrides[get_email_service] = lambda: fake_email_service
    yield application
    await application.state.engine.dispose()


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    """Sessions on the application's own engine, for inspecting what a request stored."""
    return app.state.session_factory


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
