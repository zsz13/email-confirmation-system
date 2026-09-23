"""Session-cookie login for the admin panel, built on SQLAdmin's ``AuthenticationBackend``.

A successful login stores the username in a signed session cookie; every admin request is
then checked against that cookie. SQLAdmin supplies the login page and the redirects. HTTP
Basic authentication was deliberately not used: browsers cache Basic credentials for the
whole session with no way to log out, and the password would travel with every request.
"""

import logging
import math
import secrets
import time

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from app.core.config import Settings
from app.services.rate_limit import RateLimit, SlidingWindowLimiter

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user_id"  # the key SQLAdmin's audit and authorisation hooks read
# Failed login attempts allowed per client address before the login is refused for a while;
# one shared password is otherwise guessable at line speed once the path is known.
LOGIN_FAILURES = RateLimit(max_requests=5, window_seconds=60.0)
THROTTLED_DETAIL = "Too many failed login attempts. Try again later."


class SessionAuth(AuthenticationBackend):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            secret_key=settings.admin_session_secret.get_secret_value(),
            session_cookie="admin_session",
            # Sent only to admin URLs, never with public API calls or page loads.
            path=settings.admin_path,
            max_age=settings.admin_session_max_age_seconds,
            same_site="strict",
            # Secure requires HTTPS; the plain-HTTP local demo keeps it off (see the README).
            https_only=settings.admin_cookie_secure,
        )
        self._username = settings.admin_username
        self._password = settings.admin_password.get_secret_value()
        self._failures = SlidingWindowLimiter(LOGIN_FAILURES)

    async def login(self, request: Request) -> bool | Response:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        wait = self._failures.retry_after(client, now)
        if wait is not None:
            logger.warning("admin login throttled client_ip=%s", client)
            return PlainTextResponse(
                THROTTLED_DETAIL,
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        # Constant-time comparisons, both always evaluated, so response timing reveals
        # neither the length of the secrets nor which of the two fields was wrong.
        username_ok = secrets.compare_digest(username.encode(), self._username.encode())
        password_ok = secrets.compare_digest(password.encode(), self._password.encode())
        if not (username_ok & password_ok):
            self._failures.record(client, now)
            logger.warning("admin login failed client_ip=%s", client)
            return False

        # A fresh session on login, so nothing set before authentication carries over.
        request.session.clear()
        request.session[SESSION_USER_KEY] = username
        logger.info("admin login succeeded username=%s", username)
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        # The cookie is signed, so its content is trusted once the signature checks out.
        return request.session.get(SESSION_USER_KEY) == self._username
