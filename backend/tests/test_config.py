import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings

REQUIRED = {
    "database_url": "sqlite+aiosqlite://",
    "mailjet_api_key": SecretStr("test-api-key"),
    "mailjet_secret_key": SecretStr("test-secret-key"),
    "mailjet_sender_email": "sender@example.com",
}


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        cors_origins="http://localhost:4200, http://127.0.0.1:4200",
        **REQUIRED,
    )

    assert settings.cors_origins == ["http://localhost:4200", "http://127.0.0.1:4200"]


def test_rate_limits_are_on_by_default_with_the_documented_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "RATE_LIMIT_IP_MAX_REQUESTS",
        "RATE_LIMIT_IP_WINDOW_SECONDS",
        "RATE_LIMIT_EMAIL_MAX_REQUESTS",
        "RATE_LIMIT_EMAIL_WINDOW_SECONDS",
        "RATE_LIMIT_STATUS_IP_MAX_REQUESTS",
        "RATE_LIMIT_STATUS_IP_WINDOW_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None, **REQUIRED)  # pyright: ignore[reportCallIssue]

    # Submissions: 3 a minute per client IP, 3 per ten minutes per mailbox.
    assert settings.rate_limit_ip_max_requests == 3
    assert settings.rate_limit_ip_window_seconds == 60
    assert settings.rate_limit_email_max_requests == 3
    assert settings.rate_limit_email_window_seconds == 600
    # Status reads keep their own, looser limit.
    assert settings.rate_limit_status_ip_max_requests == 60
    assert settings.rate_limit_status_ip_window_seconds == 60


def test_the_reconcile_interval_defaults_to_three_seconds_and_stays_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELIVERY_RECONCILE_INTERVAL_SECONDS", raising=False)
    assert (
        Settings(_env_file=None, **REQUIRED).delivery_reconcile_interval_seconds  # pyright: ignore[reportCallIssue]
        == 3
    )

    # A setting of its own, independent of the page's polling interval.
    monkeypatch.setenv("DELIVERY_RECONCILE_INTERVAL_SECONDS", "7")
    assert (
        Settings(_env_file=None, **REQUIRED).delivery_reconcile_interval_seconds  # pyright: ignore[reportCallIssue]
        == 7
    )


def test_cors_wildcard_is_rejected() -> None:
    with pytest.raises(ValidationError, match="explicit origins"):
        Settings(_env_file=None, cors_origins="*", **REQUIRED)  # pyright: ignore[reportCallIssue]


def test_a_settings_error_never_echoes_configured_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A startup failure is printed to the container log, so the error names the problem
    # and never quotes a setting's value: here the last one configured is a secret.
    monkeypatch.delenv("MAILJET_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://app:db-password@mysql:3306/app")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "mailjet-secret-value")
    monkeypatch.setenv("MAILJET_SENDER_EMAIL", "sender@example.com")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "session-secret-that-must-stay-out")

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    message = str(caught.value)
    assert "mailjet_api_key" in message
    for value in ("db-password", "mailjet-secret", "must-stay-out"):
        assert value not in message
