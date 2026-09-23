"""Application settings, loaded from environment variables and ``backend/.env``."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    EmailStr,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Admin panel defaults, committed on purpose for the LOCAL DEMONSTRATION of this take-home
# repository. They are public knowledge the moment the repository is; a real deployment
# overrides every one of them through the environment and serves the panel over HTTPS.
# The long random path only keeps the panel out of casual reach. It is not a secret and
# not a security boundary; the session login is the boundary.
DEMO_ADMIN_PATH = (
    "/q7Vf2mZ9Kx4Tn8Rb3Lp6Hs1Yw5Dc0Gj4Ua9Pe2Nr7Bk6Xv3Qm8Sf1Zh5Wt0Cy4Ld9Jp2En7"
)
DEMO_ADMIN_USERNAME = "demo-admin"
DEMO_ADMIN_PASSWORD = "demo-admin-2026"  # noqa: S105  # demo-only, documented in the README
DEMO_ADMIN_SESSION_SECRET = "demo-only-session-secret-replace-in-production"  # noqa: S105

# One or more plain path segments: no scheme, no host, no query, nothing to redirect through.
ADMIN_PATH_PATTERN = re.compile(r"^(/[A-Za-z0-9_-]+)+$")


class DatabaseSettings(BaseSettings):
    """The subset Alembic needs, so migrations run with only DATABASE_URL configured."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A startup error lands in the container log; by default it would quote the
        # input, which here includes the database URL and every secret.
        hide_input_in_errors=True,
    )

    database_url: str


class Settings(DatabaseSettings):
    app_name: str = "Email Confirmation System"
    log_level: LogLevel = "INFO"

    mailjet_api_key: SecretStr
    mailjet_secret_key: SecretStr
    mailjet_sender_email: EmailStr
    mailjet_sender_name: str = "Email Confirmation System"
    mailjet_send_url: HttpUrl = HttpUrl("https://api.mailjet.com/v3.1/send")
    # Message resource, read as <url>/<MessageID> to look a message's delivery state up.
    mailjet_message_url: HttpUrl = HttpUrl("https://api.mailjet.com/v3/REST/message")
    mailjet_timeout_seconds: float = 10.0
    # How often the background reconciler asks Mailjet about messages still in progress
    # (app.services.delivery_reconciler). One lookup per such message per cycle, so this is
    # also the upper bound on how often any one message is looked up. The default matches the
    # page's 3 s status polling, which reads what these lookups store; the two stay separate.
    delivery_reconcile_interval_seconds: float = Field(default=3.0, ge=1)

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:4200"]

    # Abuse protection for POST /api/v1/submissions, counted per client IP and per
    # normalised destination address. Process-local; see app.services.rate_limit.
    # Every request let through counts, whatever its email's delivery status turns out to be.
    rate_limit_ip_max_requests: int = Field(default=3, gt=0)
    rate_limit_ip_window_seconds: float = Field(default=60.0, gt=0)
    rate_limit_email_max_requests: int = Field(default=3, gt=0)
    rate_limit_email_window_seconds: float = Field(default=600.0, gt=0)
    # Per client IP for GET /api/v1/submissions/{tracking_id}/status. The frontend polls
    # every 3 s (20 a minute), so the default leaves room for a second tab and manual checks.
    rate_limit_status_ip_max_requests: int = Field(default=60, gt=0)
    rate_limit_status_ip_window_seconds: float = Field(default=60.0, gt=0)

    # Read-only admin panel (SQLAdmin), mounted at ADMIN_PATH with a session-cookie login.
    admin_path: str = DEMO_ADMIN_PATH
    admin_username: str = Field(default=DEMO_ADMIN_USERNAME, min_length=1)
    admin_password: SecretStr = SecretStr(DEMO_ADMIN_PASSWORD)
    # Signs the session cookie; anyone who knows it can forge an admin session.
    admin_session_secret: SecretStr = SecretStr(DEMO_ADMIN_SESSION_SECRET)
    admin_session_max_age_seconds: int = Field(default=8 * 60 * 60, gt=0)
    # Marks the session cookie Secure so browsers only send it over HTTPS. Off for the
    # plain-HTTP local demo; must be on in any deployment reachable over a network.
    admin_cookie_secure: bool = False

    @field_validator("admin_path")
    @classmethod
    def _validate_admin_path(cls, value: str) -> str:
        value = value.rstrip("/")
        if not ADMIN_PATH_PATTERN.fullmatch(value) or value.startswith("/api"):
            raise ValueError(
                "ADMIN_PATH must be one or more plain path segments such as /ops-panel and "
                "must not sit under the public /api tree"
            )
        return value

    @property
    def admin_uses_demo_defaults(self) -> bool:
        """True while the committed demo password or session secret is still in force."""
        return (
            self.admin_password.get_secret_value() == DEMO_ADMIN_PASSWORD
            or self.admin_session_secret.get_secret_value() == DEMO_ADMIN_SESSION_SECRET
        )

    @model_validator(mode="after")
    def _refuse_demo_admin_secrets_behind_https(self) -> Settings:
        # ADMIN_COOKIE_SECURE=true is the marker of a real (HTTPS) deployment; the committed
        # demo secrets would let anyone with the repository forge an admin session there.
        if self.admin_cookie_secure and self.admin_uses_demo_defaults:
            raise ValueError(
                "ADMIN_COOKIE_SECURE=true requires ADMIN_PASSWORD and ADMIN_SESSION_SECRET "
                "to be overridden; the committed demo values are public"
            )
        return self

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        # Lets CORS_ORIGINS be a plain comma-separated string instead of a JSON list.
        if isinstance(value, str):
            value = [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list) and "*" in value:
            raise ValueError(
                "CORS_ORIGINS must list explicit origins; '*' is not allowed"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    # Required values arrive from the environment; Pyright only sees the field signature.
    return Settings()  # pyright: ignore[reportCallIssue]
