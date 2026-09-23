"""A stand-in for the Mailjet Send API v3.1, for the automated integration test.

Accepts any POST as a successful send and answers with the shape the backend validates
(``{"Messages": [{"Status": "success", ...}]}``). Nothing is sent anywhere. Every accepted
recipient is kept in memory and listed at ``GET /messages`` so the test can check what the
backend asked to deliver. ``GET /v3/REST/message/<MessageID>`` walks each accepted message
through a short lifecycle: ``queued`` on its first lookup, ``sent`` (Mailjet's "delivered")
from then on, so the backend's background reconciler and the delivery-status check can be
seen moving a row through more than one state.
Credentials are never read or logged.

Standard library only, so it runs from the stock ``python:3.14-slim`` image.
"""

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8025
MESSAGE_PREFIX = "/v3/REST/message/"
accepted: list[dict[str, str]] = []
lookups: dict[int, int] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        # Method, path and status only: request headers (Authorization) are never written out.
        sys.stderr.write(
            f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}\n"
        )

    def _reply(self, status: HTTPStatus, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(HTTPStatus.OK, {"status": "ok"})
        elif self.path == "/messages":
            self._reply(HTTPStatus.OK, accepted)
        elif self.path.startswith(MESSAGE_PREFIX):
            self._message_status(self.path.removeprefix(MESSAGE_PREFIX))
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"detail": "not found"})

    def _message_status(self, message_id: str) -> None:
        # MessageIDs are 1..n, in the order the messages were accepted.
        if not message_id.isdigit() or not 1 <= int(message_id) <= len(accepted):
            self._reply(HTTPStatus.NOT_FOUND, {"ErrorMessage": "Object not found"})
            return
        seen = lookups[int(message_id)] = lookups.get(int(message_id), 0) + 1
        data = [{"ID": int(message_id), "Status": "queued" if seen == 1 else "sent"}]
        self._reply(HTTPStatus.OK, {"Count": 1, "Data": data, "Total": 1})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
            messages = request["Messages"]
        except (ValueError, KeyError, TypeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"ErrorMessage": "malformed request"})
            return

        results = []
        for message in messages:
            recipients = []
            for recipient in message.get("To", []):
                accepted.append(
                    {
                        "email": recipient.get("Email", ""),
                        "name": recipient.get("Name", ""),
                    }
                )
                recipients.append(
                    {"Email": recipient.get("Email", ""), "MessageID": len(accepted)}
                )
            results.append({"Status": "success", "To": recipients, "Cc": [], "Bcc": []})
        self._reply(HTTPStatus.OK, {"Messages": results})


if __name__ == "__main__":
    sys.stderr.write(f"mailjet stub listening on :{PORT}\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104  # container-internal
