import base64
import json
import logging

import httpx
import pytest

from app.core.config import Settings
from app.services.email import (
    EmailDeliveryError,
    EmailService,
    build_confirmation_email,
)

RECIPIENT = "daniyar-swe-demo-0923@mailinator.com"
SUCCESS_BODY = {
    "Messages": [
        {
            "Status": "success",
            "To": [{"Email": RECIPIENT, "MessageID": 1234, "MessageUUID": "uuid-1234"}],
        }
    ]
}


async def send_with(
    settings: Settings,
    handler: httpx.MockTransport | None = None,
    *,
    response: httpx.Response,
) -> httpx.Request:
    """Send one confirmation through a mocked transport and return the captured request."""
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    transport = handler or httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=transport) as client:
        service = EmailService(client=client, settings=settings)
        await service.send_confirmation(
            recipient_name="Daniyar", recipient_email=RECIPIENT
        )
    return captured[0]


async def test_send_confirmation_posts_expected_mailjet_request(
    settings: Settings,
) -> None:
    request = await send_with(settings, response=httpx.Response(200, json=SUCCESS_BODY))

    assert request.method == "POST"
    assert str(request.url) == "https://api.mailjet.com/v3.1/send"
    expected_auth = base64.b64encode(b"test-api-key:test-secret-key").decode()
    assert request.headers["Authorization"] == f"Basic {expected_auth}"

    message = json.loads(request.content)["Messages"][0]
    assert message["From"] == {
        "Email": "sender@example.com",
        "Name": "Email Confirmation System",
    }
    assert message["To"] == [{"Email": RECIPIENT, "Name": "Daniyar"}]
    assert message["Subject"] == "Email Confirmation"
    for part in (message["TextPart"], message["HTMLPart"]):
        assert "Daniyar" in part
        assert RECIPIENT in part


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            httpx.Response(
                401, json={"ErrorMessage": "API key authentication failure"}
            ),
            id="authentication-failure",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "Messages": [
                        {
                            "Status": "error",
                            "Errors": [{"ErrorMessage": "Invalid recipient"}],
                        }
                    ]
                },
            ),
            id="message-level-error",
        ),
        pytest.param(httpx.Response(500, text="upstream exploded"), id="server-error"),
        pytest.param(
            httpx.Response(200, json={"Messages": []}), id="empty-message-list"
        ),
        pytest.param(
            httpx.Response(200, text="<html>not json</html>"), id="unexpected-body"
        ),
    ],
)
async def test_non_success_responses_raise_delivery_error(
    settings: Settings, response: httpx.Response
) -> None:
    with pytest.raises(EmailDeliveryError):
        await send_with(settings, response=response)


async def test_success_without_recipient_details_is_still_accepted(
    settings: Settings,
) -> None:
    # Mailjet's per-recipient block is informational; its absence must not fail a sent email.
    body = {"Messages": [{"Status": "success", "To": []}]}

    await send_with(settings, response=httpx.Response(200, json=body))


@pytest.mark.parametrize(
    ("recipients", "expected"),
    [
        ([{"Email": RECIPIENT, "MessageID": 1234}], 1234),
        ([{"Email": RECIPIENT, "MessageID": "1234"}], None),
        ([{"Email": RECIPIENT, "MessageID": True}], None),
        ([{"Email": RECIPIENT}], None),
        ([], None),
    ],
)
async def test_the_accepted_message_id_is_returned_when_mailjet_gives_one(
    settings: Settings, recipients: list[dict[str, object]], expected: int | None
) -> None:
    body = {"Messages": [{"Status": "success", "To": recipients}]}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))

    async with httpx.AsyncClient(transport=transport) as client:
        message_id = await EmailService(
            client=client, settings=settings
        ).send_confirmation(recipient_name="Daniyar", recipient_email=RECIPIENT)

    assert message_id == expected


async def test_network_failure_raises_delivery_error(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(EmailDeliveryError):
        await send_with(
            settings, httpx.MockTransport(refuse), response=httpx.Response(200)
        )


def test_confirmation_email_names_recipient_and_exact_address() -> None:
    content = build_confirmation_email(
        "Daniyar", RECIPIENT, "Email Confirmation System"
    )

    assert content.subject == "Email Confirmation"
    assert "Hello Daniyar," in content.text
    assert RECIPIENT in content.text
    assert "Hello Daniyar," in content.html
    assert RECIPIENT in content.html
    assert "Email Confirmation System" in content.text


def test_confirmation_email_escapes_html_in_user_input() -> None:
    content = build_confirmation_email("<b>Bob</b>", "bob+<tag>@example.com", "Sender")

    assert "&lt;b&gt;Bob&lt;/b&gt;" in content.html
    assert "<b>Bob</b>" not in content.html
    assert "bob+&lt;tag&gt;@example.com" in content.html
    assert "<b>Bob</b>" in content.text


# --- what delivery writes to the log ----------------------------------------------------


async def test_a_successful_delivery_is_logged_with_a_masked_recipient(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        await send_with(settings, response=httpx.Response(200, json=SUCCESS_BODY))

    assert any(
        record.levelno == logging.INFO
        and "email accepted by provider" in record.getMessage()
        and "recipient=d***@mailinator.com" in record.getMessage()
        for record in caplog.records
    )
    assert RECIPIENT not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            httpx.Response(
                400,
                json={
                    "ErrorIdentifier": "3f1c9e0a",
                    "ErrorCode": "mj-0013",
                    "StatusCode": 400,
                    "ErrorMessage": f'"{RECIPIENT}" is an invalid email address.',
                    "ErrorRelatedTo": ["To[0].Email"],
                },
            ),
            id="rejected-by-http-status",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "Messages": [
                        {
                            "Status": "error",
                            "To": [{"Email": RECIPIENT}],
                            "Errors": [
                                {
                                    "ErrorCode": "mj-0013",
                                    "ErrorMessage": f"{RECIPIENT} was refused",
                                    "ErrorRelatedTo": ["To[0].Email"],
                                },
                            ],
                        }
                    ]
                },
            ),
            id="rejected-per-message",
        ),
        pytest.param(
            httpx.Response(200, text=f"<html>{RECIPIENT} bounced</html>"),
            id="unexpected-body",
        ),
    ],
)
async def test_a_provider_error_body_is_never_logged(
    settings: Settings, response: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    """The provider's text quotes the request back; only its codes reach the log."""
    with caplog.at_level(logging.INFO, logger="app"), pytest.raises(EmailDeliveryError):
        await send_with(settings, response=response)

    assert "email delivery failed" in caplog.text
    assert RECIPIENT not in caplog.text
    assert "mailinator.com" not in caplog.text
    for provider_text in ("invalid email address", "was refused", "bounced", "<html>"):
        assert provider_text not in caplog.text


@pytest.mark.parametrize(
    ("body", "summary"),
    [
        (
            {"ErrorCode": "mj-0002", "ErrorMessage": "API key authentication failure"},
            "error_codes=mj-0002 related_to=-",
        ),
        (
            {
                "Messages": [
                    {
                        "Status": "error",
                        "Errors": [
                            {"ErrorCode": "mj-0013", "ErrorRelatedTo": ["To[0].Email"]},
                            {"ErrorCode": "mj-0004", "ErrorRelatedTo": ["From.Email"]},
                        ],
                    }
                ]
            },
            "error_codes=mj-0004,mj-0013 related_to=From.Email,To[0].Email",
        ),
        (
            {"ErrorCode": "evil code\nCRITICAL", "ErrorRelatedTo": ["a b", 7]},
            "error_codes=- related_to=-",
        ),
    ],
)
async def test_only_mailjet_error_codes_and_field_names_are_logged(
    settings: Settings,
    body: dict[str, object],
    summary: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app"), pytest.raises(EmailDeliveryError):
        await send_with(settings, response=httpx.Response(400, json=body))

    assert f"Mailjet returned HTTP 400 {summary}" in caplog.text
