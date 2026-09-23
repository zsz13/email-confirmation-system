"""Process-local sliding-window rate limiting for the submissions endpoint.

The windows live in this process's memory, which is exactly right for a single-instance demo:
no extra infrastructure, and the state disappears with the process. A horizontally scaled
deployment would keep the same windows in a store shared by every instance, such as Redis,
so that the limits hold across instances.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

SWEEP_EVERY = 1024  # recorded events between full sweeps of expired keys


@dataclass(frozen=True)
class RateLimit:
    """At most ``max_requests`` within any ``window_seconds`` long window."""

    max_requests: int
    window_seconds: float


class SlidingWindowLimiter:
    """One rate limit applied independently to each key (an IP address, an email...)."""

    def __init__(self, limit: RateLimit) -> None:
        self._limit = limit
        self._events: dict[str, deque[float]] = {}
        self._recorded = 0

    def retry_after(self, key: str, now: float) -> float | None:
        """Seconds until ``key`` is allowed again, or ``None`` if it is allowed right now."""
        events = self._events.get(key)
        if events is None:
            return None
        self._expire(events, now)
        if not events:
            del self._events[key]
            return None
        if len(events) < self._limit.max_requests:
            return None
        return events[0] + self._limit.window_seconds - now

    def record(self, key: str, now: float) -> None:
        self._events.setdefault(key, deque()).append(now)
        self._recorded += 1
        if self._recorded % SWEEP_EVERY == 0:
            self._sweep(now)

    def _expire(self, events: deque[float], now: float) -> None:
        cutoff = now - self._limit.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()

    def _sweep(self, now: float) -> None:
        # Keys that were never checked again would otherwise keep their expired events forever.
        for key, events in list(self._events.items()):
            self._expire(events, now)
            if not events:
                del self._events[key]


GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalize_email(email: str) -> str:
    """Key under which one mailbox is counted for rate limiting.

    Used only to build the limiter key: the address that is validated, stored and mailed is
    never modified. Surrounding whitespace is trimmed and the domain is lower-cased for
    every address, because domains are case-insensitive.

    Gmail is the one provider whose aliasing rules are well documented: the local part is
    case-insensitive, dots in it are ignored, ``+tag`` suffixes route to the same inbox, and
    ``googlemail.com`` is the same service as ``gmail.com``. So ``John.Smith+demo@gmail.com``
    and ``johnsmith@gmail.com`` share one budget. Every other domain keeps its local part
    exactly as written, since ``user+one@`` and ``user+two@`` may well be different
    mailboxes there.
    """
    local, _, domain = email.strip().rpartition("@")
    domain = domain.lower()
    if domain in GMAIL_DOMAINS:
        local = local.lower().partition("+")[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


class SubmissionRateLimiter:
    """Per-client-IP and per-destination-email limits for creating submissions."""

    def __init__(
        self,
        *,
        per_ip: RateLimit,
        per_email: RateLimit,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._per_ip = SlidingWindowLimiter(per_ip)
        self._per_email = SlidingWindowLimiter(per_email)

    def try_acquire(self, *, client_ip: str | None, email: str) -> int | None:
        """Consume one request for both keys, or return the ``Retry-After`` seconds.

        ``None`` means the request may proceed. A rejected request consumes nothing, so the
        returned wait is accurate. Checking and recording happen in one synchronous step with
        no ``await`` in between, so concurrent requests on the event loop cannot interleave
        and slip past the limit.

        When the server does not know the peer address (``client_ip`` is ``None``) only the
        per-address limit applies; lumping every client into one bucket would look like an
        outage rather than protection.
        """
        now = self._clock()
        email_key = normalize_email(email)
        waits = [self._per_email.retry_after(email_key, now)]
        if client_ip is not None:
            waits.append(self._per_ip.retry_after(client_ip, now))
        pending = [wait for wait in waits if wait is not None]
        if pending:
            return max(1, math.ceil(max(pending)))
        if client_ip is not None:
            self._per_ip.record(client_ip, now)
        self._per_email.record(email_key, now)
        return None


class ClientRateLimiter:
    """One per-client-IP limit, for endpoints with no second key to count.

    Used for the public delivery-status lookup: generous enough for a page polling every
    few seconds, tight enough that nobody can sweep tracking ids from one address.
    """

    def __init__(
        self, limit: RateLimit, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._per_ip = SlidingWindowLimiter(limit)

    def try_acquire(self, client_ip: str | None) -> int | None:
        """Consume one request, or return the ``Retry-After`` seconds; ``None`` = proceed.

        An unknown peer address is not limited, for the same reason as in
        :meth:`SubmissionRateLimiter.try_acquire`.
        """
        if client_ip is None:
            return None
        now = self._clock()
        wait = self._per_ip.retry_after(client_ip, now)
        if wait is not None:
            return max(1, math.ceil(wait))
        self._per_ip.record(client_ip, now)
        return None
