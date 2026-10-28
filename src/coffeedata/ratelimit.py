"""One request budget shared by every store on a platform that rate-limits by client IP (Shopify).

PoliteClient spaces requests per host, which is polite to each store but not to a platform: 1,100 Shopify
stores are one rate limiter to Shopify, and 32 workers each politely visiting a different store add up to a
burst it answers with 429s for everyone (run 2026-10-28: 14% of Shopify fetches ok). A PlatformLimiter
caps that platform's whole request rate and concurrency, and a 429 from any of its stores pauses all of
them (honoring Retry-After) and halves the rate until requests succeed again.
"""

from __future__ import annotations

import email.utils
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx

from .http import FetchError

log = logging.getLogger(__name__)

# 3 requests/s: the 2026-10-26 run started up to ~90 Shopify stores a minute (robots.txt + one page each,
# about 3 requests/s) with 99% ok; the 2026-10-28 run, with one more page per store and 32 workers, went past
# 4/s plus retries and was blocked. ~1,100 stores x ~3.3 requests (robots.txt, ~2.3 pages) is ~3,600
# requests: about 20 minutes at 3/s, alongside the other platforms.
SHOPIFY_RPS = 3.0
SHOPIFY_CONCURRENCY = 8


class RateLimited(FetchError):
    """The platform kept answering 429 for longer than the limiter waits: fail fast, retry in a later pass."""


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After as seconds: delta-seconds or an HTTP date. None if missing or unreadable."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _host(response: httpx.Response) -> str:
    try:
        return response.request.url.host
    except RuntimeError:  # a response built without a request (tests)
        return "?"


class PlatformLimiter:
    def __init__(
        self,
        name: str,
        rate: float,
        max_concurrent: int,
        default_cooldown: float = 20.0,  # a 429 without Retry-After
        max_cooldown: float = 120.0,
        max_slowdown: float = 8.0,  # after 429s the rate drops to as little as rate / max_slowdown
        give_up_after: float = 900.0,  # only 429s for this long (Retry-After can understate a block): stop
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0 or max_concurrent < 1:
            raise ValueError("rate must be > 0 and max_concurrent >= 1")
        self.name = name
        self.base_interval = 1.0 / rate
        self.interval = self.base_interval
        self.default_cooldown = default_cooldown
        self.max_cooldown = max_cooldown
        self.max_interval = self.base_interval * max_slowdown
        self.give_up_after = give_up_after
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._next = 0.0  # earliest start of the next request
        self.cooldown_until = 0.0
        self._blocked_since: float | None = None  # first 429 since the last success
        self.stats = {"requests": 0, "rate_limited": 0, "cooldowns": 0}

    def _check_blocked(self, now: float) -> None:
        if self._blocked_since is not None and now - self._blocked_since >= self.give_up_after:
            raise RateLimited(f"{self.name}: only 429s for {now - self._blocked_since:.0f}s, giving up")

    def _wait_for_slot(self) -> None:
        """Sleep until this caller may start a request: past any cooldown and one interval after the last."""
        while True:
            with self._lock:
                now = self._clock()
                self._check_blocked(now)
                start = max(now, self._next, self.cooldown_until)
                if start <= now:
                    self._next = now + self.interval
                    return
                self._next = start + self.interval  # reserve; re-checked on waking (a cooldown may begin)
            self._sleep(start - now)
            with self._lock:
                now = self._clock()
                if now >= self.cooldown_until:
                    self._check_blocked(now)
                    return

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one of the platform's request slots for the duration of one request."""
        while True:
            self._wait_for_slot()
            self._sem.acquire()
            with self._lock:
                if self._clock() >= self.cooldown_until:  # no cooldown began while we queued
                    self.stats["requests"] += 1
                    break
            self._sem.release()
        try:
            yield
        finally:
            self._sem.release()

    def record(self, response: httpx.Response) -> None:
        """Learn from a response: a 429 pauses the whole platform and slows it; anything else speeds it up."""
        with self._lock:
            now = self._clock()
            if response.status_code != 429:
                self._blocked_since = None
                self.interval = max(self.base_interval, self.interval * 0.97)
                return
            self.stats["rate_limited"] += 1
            if self._blocked_since is None:
                self._blocked_since = now
            wait = retry_after_seconds(response.headers.get("retry-after"))
            wait = min(self.max_cooldown, self.default_cooldown if wait is None else wait)
            self.interval = min(self.max_interval, self.interval * 2)
            if now + wait > self.cooldown_until:
                if now >= self.cooldown_until:
                    self.stats["cooldowns"] += 1
                    log.warning(
                        "%s: 429 from %s; pausing all %s requests %.0fs, then %.2f requests/s",
                        self.name,
                        _host(response),
                        self.name,
                        wait,
                        1 / self.interval,
                    )
                self.cooldown_until = now + wait
