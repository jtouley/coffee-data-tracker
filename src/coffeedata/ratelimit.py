"""One request budget shared by every store on a platform that rate-limits by client IP (Shopify).

PoliteClient spaces requests per host, which is polite to each store but not to a platform: 1,100 Shopify
stores are one rate limiter to Shopify, and 32 workers each politely visiting a different store add up to a
burst it answers with 429s for everyone (run 2026-10-28: 14% of Shopify fetches ok). A PlatformLimiter
caps that platform's whole request rate and concurrency, and a block (429, or Shopify's 430 and 503) from
any of its stores pauses all of them (honoring Retry-After) and halves the rate until requests succeed again.

Per-host mode (POC, run 2026-10-30: 182 platform-wide pauses from 146 stores, timed out at 240 min): most 429s
are one store's own limit, so a block pauses only that store, and the platform pauses only on a 430 or when many
of the stores answering at once block (burst_hosts, burst_share, burst_window). A store asked to wait longer than
defer_after is parked (HostRateLimited) and ingest.run retries it after the main pass.
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

from .http import BLOCK_STATUSES, FetchError

log = logging.getLogger(__name__)

# 3 requests/s: the 2026-10-26 run started up to ~90 Shopify stores a minute (robots.txt + one page each,
# about 3 requests/s) with 99% ok; the 2026-10-28 run, with one more page per store and 32 workers, went past
# 4/s plus retries and was blocked. ~1,100 stores x ~3.3 requests (robots.txt, ~2.3 pages) is ~3,600
# requests: about 20 minutes at 3/s, alongside the other platforms.
# Every redirect hop uses the same platform budget, alongside per-host spacing.
SHOPIFY_RPS = 3.0
SHOPIFY_CONCURRENCY = 8


class RateLimited(FetchError):
    """The platform kept blocking for longer than the limiter waits: fail fast, retry in a later pass."""


class HostRateLimited(RateLimited):
    """One store asked us to wait (429/503 + Retry-After) longer than defer_after: the store is parked and
    retried after the main pass (ingest.run), while the platform's other stores carry on."""

    def __init__(self, message: str, host: str, wait: float) -> None:
        super().__init__(message)
        self.host = host
        self.wait = wait


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
        give_up_after: float = 900.0,  # only blocks for this long (Retry-After can understate a block): stop
        probe_every: float = 60.0,  # once stopped, let one request through this often to see if it lifted
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        # Per-host mode: a block pauses only the store that sent it (for its Retry-After); the whole platform
        # pauses and slows only when, within burst_window, at least burst_hosts distinct stores blocked and they
        # are at least burst_share of the stores that answered, or on a 430 (Shopify's IP-wide block). A store
        # whose pause is longer than defer_after raises HostRateLimited (when defer is on) instead of holding a
        # worker.
        per_host: bool = False,
        burst_hosts: int = 5,
        burst_share: float = 0.5,
        burst_window: float = 30.0,
        defer_after: float = 10.0,
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
        self.probe_every = probe_every
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._next = 0.0  # earliest start of the next request
        self.cooldown_until = 0.0
        # Bumped when cooldown_until moves: a waiter's reservation from before is void.
        self._cooldowns_set = 0
        self._blocked_since: float | None = None  # first block since the last success
        self._probe_at: float | None = None  # once given up: when the next probe request may go
        self.per_host = per_host
        self.burst_hosts = burst_hosts
        self.burst_window = burst_window
        self.burst_share = burst_share
        self._answered: dict[str, float] = {}  # host -> when it last answered without a block
        self.defer_after = defer_after
        # ingest's deferred rounds turn this off: wait out a store's pause instead of parking it. Those rounds
        # retry only stores already known to block, so their 429s say nothing about the platform: they never
        # count towards a platform-wide pause (live test 2026-10-31: 6 false pauses, down to 0.25 req/s).
        self.defer = True
        self.host_until: dict[str, float] = {}
        self._blocked: dict[str, float] = {}  # host -> when it last blocked, for burst detection
        self.stats = {"requests": 0, "rate_limited": 0, "cooldowns": 0}
        if per_host:
            self.stats |= {"host_pauses": 0, "deferred": 0}

    def _check_blocked(self, now: float) -> bool:
        """Past give_up_after, fail fast, except for one probe request every probe_every (half-open). True when
        this caller is the probe: it is claimed here, under the lock, so callers that come while it waits out a
        cooldown fail fast instead of each holding a slot through the cooldown and failing after it."""
        if self._blocked_since is None or now - self._blocked_since < self.give_up_after:
            return False
        if self._probe_at is None:
            self._probe_at = now + self.probe_every
        if now < self._probe_at:
            raise RateLimited(f"{self.name}: only blocks for {now - self._blocked_since:.0f}s, giving up")
        self._probe_at = now + self.probe_every
        return True

    def _wait_for_slot(self) -> None:
        """Sleep until this caller may start a request: past any cooldown and one interval after the last."""
        probe = False
        while True:
            with self._lock:
                now = self._clock()
                probe = probe or self._check_blocked(now)
                start = max(now, self._next, self.cooldown_until)
                if (
                    probe
                ):  # the next probe is due probe_every after this one is sent, not after it was claimed
                    self._probe_at = start + self.probe_every
                if start <= now:
                    self._next = now + self.interval
                    self.stats["requests"] += 1
                    return
                self._next = start + self.interval  # reserve; re-checked on waking (a cooldown may begin)
                cooldowns_set = self._cooldowns_set
            self._sleep(start - now)
            with self._lock:
                now = self._clock()
                if self._cooldowns_set == cooldowns_set:  # no cooldown began, so the reservation stands
                    if not probe:
                        self._check_blocked(now)
                    self.stats["requests"] += 1
                    return

    def _wait_for_host(self, host: str | None) -> None:
        """Per-host mode: wait out this store's own pause, or (defer on, pause > defer_after) park the store."""
        if not self.per_host or host is None:
            return
        while True:
            with self._lock:
                wait = self.host_until.get(host, 0.0) - self._clock()
                if wait <= 0:
                    return
                if self.defer and wait > self.defer_after:
                    self.stats["deferred"] += 1
                    raise HostRateLimited(f"{host} asked to wait {wait:.0f}s; deferred", host, wait)
            self._sleep(wait)

    @contextmanager
    def slot(self, host: str | None = None) -> Iterator[None]:
        """Hold one of the platform's request slots for the duration of one request."""
        self._wait_for_host(host)  # before taking a slot: a paused store never holds one
        # The slot first, then the time: callers queued for a slot can't bank rate-spacing time and then all
        # start at once when slots free up together.
        self._sem.acquire()
        try:
            self._wait_for_slot()
        except BaseException:
            self._sem.release()
            raise
        try:
            yield
        finally:
            self._sem.release()

    def record(self, response: httpx.Response) -> None:
        """Learn from a response: a block pauses the whole platform and slows it; a 2xx or 404 speeds it up;
        any other status (a 403, a 500) says nothing about the platform's limit."""
        status = response.status_code
        with self._lock:
            now = self._clock()
            if self.per_host and status not in BLOCK_STATUSES:
                self._answered[_host(response)] = now
            if 200 <= status < 300 or status in (404, 410):
                self._blocked_since = None
                self._probe_at = None
                self.interval = max(self.base_interval, self.interval * 0.97)
                return
            if status not in BLOCK_STATUSES:
                return
            self.stats["rate_limited"] += 1
            wait = retry_after_seconds(response.headers.get("retry-after"))
            wait = min(self.max_cooldown, self.default_cooldown if wait is None else wait)
            if self.per_host and status != 430:
                host = _host(response)
                self.host_until[host] = max(self.host_until.get(host, 0.0), now + wait)
                if not self.defer:  # a deferred round: this store's own pause only
                    self.stats["host_pauses"] += 1
                    return
                self._blocked[host] = now
                cut = now - self.burst_window
                for d in (self._blocked, self._answered):
                    for h in [h for h, t in d.items() if t < cut]:
                        del d[h]
                blocked = len(self._blocked)
                answered = len(self._answered.keys() - self._blocked.keys())
                if blocked < self.burst_hosts or blocked < self.burst_share * (blocked + answered):
                    self.stats["host_pauses"] += 1
                    log.info("%s: %d from %s; pausing that store %.0fs", self.name, status, host, wait)
                    return
                log.warning(
                    "%s: %d of %d stores blocked within %.0fs: platform-wide",
                    self.name,
                    blocked,
                    blocked + answered,
                    self.burst_window,
                )
                self._blocked.clear()
            if self._blocked_since is None:
                self._blocked_since = now
            if now + wait > self.cooldown_until:
                if now >= self.cooldown_until:  # a new block, not another request that was in flight with it
                    self.interval = min(self.max_interval, self.interval * 2)
                    self.stats["cooldowns"] += 1
                    log.warning(
                        "%s: %d from %s; pausing all %s requests %.0fs, then %.2f requests/s",
                        self.name,
                        status,
                        _host(response),
                        self.name,
                        wait,
                        1 / self.interval,
                    )
                self.cooldown_until = now + wait
                self._next = self.cooldown_until  # reservations made before the cooldown are void
                self._cooldowns_set += 1
