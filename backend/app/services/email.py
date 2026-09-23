"""Confirmation email delivery through the Mailjet Send API v3.1."""

import html
import logging
import re
from dataclasses import dataclass
from http import HTTPStatus

import httpx

from app.core.config import Settings
from app.core.logging import mask_email

logger = logging.getLogger(__name__)

# What of a Mailjet error body may be logged: its error codes and the names of the fields they
# point at. Never the body itself: its messages quote the request back, address included.
SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9._-]{1,40}")
SAFE_FIELD_NAME = re.compile(r"[A-Za-z0-9_.\[\]]{1,60}")


class EmailDeliveryError(Exception):
    """The confirmation email could not be delivered. Safe to surface to API clients."""


@dataclass(frozen=True, slots=True)
class EmailContent:
    subject: str
    text: str
    html: str


def build_confirmation_email(
    recipient_name: str, recipient_email: str, sender_name: str
) -> EmailContent:
    """Render the plain-text and HTML bodies. Both name the recipient and the exact address."""
    text = (
        f"Hello {recipient_name},\n"
        "\n"
        "Thank you for submitting your email address.\n"
        "\n"
        "This confirmation email was sent to:\n"
        f"{recipient_email}\n"
        "\n"
        "Your email address has been successfully recorded.\n"
        "\n"
        "Best,\n"
        f"{sender_name}\n"
    )
    safe_name = html.escape(recipient_name)
    safe_email = html.escape(recipient_email)
    safe_sender = html.escape(sender_name)
    html_body = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        '  <body style="font-family: Arial, Helvetica, sans-serif; color: #1f2933; line-height: 1.5;">\n'
        f"    <p>Hello {safe_name},</p>\n"
        "    <p>Thank you for submitting your email address.</p>\n"
        f"    <p>This confirmation email was sent to:<br><strong>{safe_email}</strong></p>\n"
        "    <p>Your email address has been successfully recorded.</p>\n"
        f"    <p>Best,<br>{safe_sender}</p>\n"
        "  </body>\n"
        "</html>\n"
    )
    return EmailContent(subject="Email Confirmation", text=text, html=html_body)


class EmailService:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._send_url = str(settings.mailjet_send_url)
        self._auth = httpx.BasicAuth(
            settings.mailjet_api_key.get_secret_value(),
            settings.mailjet_secret_key.get_secret_value(),
        )
        self._sender_email = str(settings.mailjet_sender_email)
        self._sender_name = settings.mailjet_sender_name

    async def send_confirmation(
        self, *, recipient_name: str, recipient_email: str
    ) -> int | None:
        """Hand the confirmation email to Mailjet or raise ``EmailDeliveryError``.

        Returns the MessageID Mailjet assigned, when it reported one. Acceptance is all a
        successful return means: Mailjet has the message, the recipient's server does not yet.
        """
        content = build_confirmation_email(
            recipient_name, recipient_email, self._sender_name
        )
        logger.debug(
            "sending confirmation email recipient=%s send_url=%s",
            mask_email(recipient_email),
            self._send_url,
        )
        payload = {
            "Messages": [
                {
                    "From": {"Email": self._sender_email, "Name": self._sender_name},
                    "To": [{"Email": recipient_email, "Name": recipient_name}],
                    "Subject": content.subject,
                    "TextPart": content.text,
                    "HTMLPart": content.html,
                }
            ]
        }
        try:
            response = await self._client.post(
                self._send_url, json=payload, auth=self._auth
            )
        except httpx.HTTPError as exc:
            # httpx error text may name the URL but never the Authorization header.
            logger.warning(
                "email delivery failed before a response was received: %s", exc
            )
            raise EmailDeliveryError("Mailjet request failed") from exc

        message_id = _accepted_message_id(response)
        logger.info(
            "email accepted by provider recipient=%s message_id=%s",
            mask_email(recipient_email),
            message_id,
        )
        return message_id


def _accepted_message_id(response: httpx.Response) -> int | None:
    """Validate a Mailjet v3.1 send response and return the accepted message's ID, if any.

    Mailjet answers HTTP 200 with ``{"Messages": [{"Status": "success", ...}]}`` when the
    message was accepted; anything else (auth failure, rejected recipient, malformed body)
    is treated as a delivery failure.
    """
    if response.status_code != HTTPStatus.OK:
        logger.warning(
            "email delivery failed: Mailjet returned HTTP %s %s",
            response.status_code,
            _error_summary(response),
        )
        raise EmailDeliveryError(f"Mailjet returned HTTP {response.status_code}")

    try:
        first = response.json()["Messages"][0]
        status = first["Status"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "email delivery failed: unexpected Mailjet response body (%d bytes, not logged)",
            len(response.content),
        )
        raise EmailDeliveryError("Unexpected Mailjet response") from exc

    if status != "success":
        logger.warning(
            "email delivery failed: Mailjet reported message status %r %s",
            str(status)[:20],
            _error_summary(response),
        )
        raise EmailDeliveryError(f"Mailjet reported message status {status!r}")

    # Needed only to look the delivery state up later; a missing or odd id does not undo
    # an accepted send, so it is recorded as unknown rather than treated as a failure.
    recipients = first.get("To")
    if isinstance(recipients, list) and recipients and isinstance(recipients[0], dict):
        message_id = recipients[0].get("MessageID")
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            return message_id
    return None


def _error_summary(response: httpx.Response) -> str:
    """``error_codes=... related_to=...`` from a Mailjet error body, and nothing else.

    Mailjet reports errors at the top level (authentication, malformed requests) or per
    message under ``Messages[].Errors[]``. Values that do not look like a code or a field
    name are dropped rather than logged.
    """
    try:
        body = response.json()
    except ValueError:
        return f"(body of {len(response.content)} bytes not logged)"
    errors = [body] if isinstance(body, dict) else []
    messages = body.get("Messages") if isinstance(body, dict) else None
    for message in messages if isinstance(messages, list) else []:
        nested = message.get("Errors") if isinstance(message, dict) else None
        errors.extend(e for e in nested or [] if isinstance(e, dict))
    codes = {
        e["ErrorCode"]
        for e in errors
        if isinstance(e.get("ErrorCode"), str)
        and SAFE_ERROR_CODE.fullmatch(e["ErrorCode"])
    }
    fields = {
        field
        for e in errors
        if isinstance(e.get("ErrorRelatedTo"), list)
        for field in e["ErrorRelatedTo"]
        if isinstance(field, str) and SAFE_FIELD_NAME.fullmatch(field)
    }
    return (
        f"error_codes={','.join(sorted(codes)) or '-'} "
        f"related_to={','.join(sorted(fields)) or '-'}"
    )
