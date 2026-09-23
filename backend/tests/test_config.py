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
