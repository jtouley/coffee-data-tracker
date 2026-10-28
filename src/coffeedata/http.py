"""A polite HTTP client: identifies itself, honors robots.txt, spaces requests per host, retries."""

from __future__ import annotations

import copy
import logging
import threading
import time
import urllib.robotparser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from .ratelimit import PlatformLimiter

log = logging.getLogger(__name__)

USER_AGENT = "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker)"
# "Slow down": 429, Shopify's 430 (an IP-wide security block), and a 503 that can mean the same.
BLOCK_STATUSES = (429, 430, 503)
RETRY_STATUSES = (429, 430, 500, 502, 503, 504)
# A robots.txt that didn't answer is remembered this long, so the host's fetches fail fast instead of each asking
# again. A site whose robots.txt always answers 5xx is never crawled: by design (RFC 9309 allows a 5xx to mean
# "disallow everything", and Google treats it so); we never fetch without a store's rules.
ROBOTS_RETRY_AFTER = 900.0


class FetchError(Exception):
    """A catalog request failed (non-200, bad JSON, blocked). The roaster's fetch is recorded as an error."""


class Blocked(FetchError):
    """robots.txt disallows the URL."""


class RobotsUnavailable(FetchError, httpx.HTTPError):
    """robots.txt didn't answer (429, 430, 5xx, or a network error) after the client's retries, so nothing on that
    host is fetched for ROBOTS_RETRY_AFTER. Callers treat it like a network error: this fetch fails and a later one
    asks again. unreachable: the host itself couldn't be reached (DNS, refused connection), not just robots.txt."""

    def __init__(self, message: str, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


class NotCatalog(FetchError):
    """HTTP 200, but the body isn't the JSON a catalog endpoint returns (e.g. an HTML or challenge page)."""


class PoliteClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        timeout: float = 15.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        limiters: dict[str, PlatformLimiter] | None = None,
    ):
        self.min_interval = min_interval
        # Platform-wide budgets (ratelimit.py), keyed by adapter name; for_platform() applies one.
        self.limiters = limiters or {}
        self._limiter: PlatformLimiter | None = None
        self.retries = retries
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=httpx.Timeout(timeout, pool=120.0),
            follow_redirects=True,
            transport=transport,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_down: dict[str, float] = {}  # host -> when to ask its robots.txt again
        self._robots_down_why: dict[str, RobotsUnavailable] = {}
        self._robots_locks: dict[str, threading.Lock] = {}  # one robots.txt request per host at a time
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def for_platform(self, name: str) -> PoliteClient:
        """This client with `name`'s platform-wide limiter applied to every request it sends (robots.txt too),
        on top of per-host spacing. A view sharing the connection pool and state: close the original, not it."""
        limiter = self.limiters.get(name)
        if limiter is None or limiter is self._limiter:
            return self
        view = copy.copy(self)
        view._limiter = limiter
        return view

    def _send(self, url: str, params: dict | None = None) -> httpx.Response:
        if self._limiter is None:
            return self._client.get(url, params=params)
        with self._limiter.slot():
            r = self._client.get(url, params=params)
            self._limiter.record(r)  # inside the slot: a 429's cooldown is set before the next request starts
        return r

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        with self._lock:
            host_lock = self._robots_locks.setdefault(host, threading.Lock())
        with host_lock:  # concurrent first requests to a host share one robots.txt request
            with self._lock:
                known = host in self._robots
                down_until = self._robots_down.get(host, 0.0)
                why = self._robots_down_why.get(host)
            if not known:
                if time.monotonic() < down_until and why is not None:
                    raise RobotsUnavailable(f"{why} (recently); not fetching without it", why.unreachable)
                self._robots[host] = self._fetch_robots(host)  # raises RobotsUnavailable: nothing is fetched
        rp = self._robots[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def _fetch_robots(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        """The host's rules; None if it has no robots.txt (404 and the like: no restrictions). Anything that isn't an
        answer about robots.txt, a 429, 430, 5xx or a network error, raises RobotsUnavailable and is remembered for
        ROBOTS_RETRY_AFTER: nothing is ever fetched from a host without its rules."""
        parser = urllib.robotparser.RobotFileParser()
        try:
            r = self._send_retrying(f"https://{host}/robots.txt", spaced=False)
        except httpx.HTTPError as e:
            unreachable = isinstance(
                e, httpx.ConnectError
            )  # DNS failure or refused connection: the host is down
            err = RobotsUnavailable(f"robots.txt for {host} failed ({type(e).__name__})", unreachable)
        else:
            if r.status_code == 200:
                parser.parse(r.text.splitlines())
                return parser
            if r.status_code in (401, 403):
                parser.disallow_all = True  # type: ignore[attr-defined]  # urllib.robotparser's own flag
                return parser
            if r.status_code not in BLOCK_STATUSES and r.status_code < 500:
                return None
            err = RobotsUnavailable(f"robots.txt for {host} answered {r.status_code}")
        # This fetch fails (the retry pass tries again) and the host stays unknown, asked again after
        # ROBOTS_RETRY_AFTER, so a site whose robots.txt never answers is never crawled.
        with self._lock:
            self._robots_down[host] = time.monotonic() + ROBOTS_RETRY_AFTER
            self._robots_down_why[host] = err
        raise RobotsUnavailable(f"{err}; not fetching without it", err.unreachable)

    def _wait_turn(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + self.min_interval
            self._last[host] = max(now, due)
        if due > now:
            time.sleep(due - now)

    def _send_retrying(self, url: str, params: dict | None = None, spaced: bool = True) -> httpx.Response:
        """_send with the client's retries: the last response (a RETRY_STATUSES one once retries run out), or the
        last attempt's network error. spaced: per-host spacing before each attempt."""
        host = urlparse(url).netloc
        for attempt in range(self.retries):
            last = attempt == self.retries - 1
            if spaced:
                self._wait_turn(host)
            retry_after = ""
            try:
                r = self._send(url, params)
            except httpx.HTTPError:
                if last:
                    raise
            else:
                if r.status_code not in RETRY_STATUSES or last:
                    return r
                if self._limiter is not None and r.status_code in BLOCK_STATUSES:
                    continue  # the limiter's platform-wide cooldown is the wait; don't idle this worker too
                retry_after = r.headers.get("retry-after", "")
            time.sleep(min(int(retry_after), 30) if retry_after.isdigit() else 2**attempt)
        raise ValueError("retries must be >= 1")

    def get(self, url: str, **params: str | int | float | bool | None) -> httpx.Response:
        if not self._allowed(url):
            raise Blocked(url)
        r = self._send_retrying(url, params or None)
        if r.status_code in RETRY_STATUSES:
            raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
        return r

    def get_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """JSON body on a 200 with a JSON payload, else None (for probing, where a miss is an answer)."""
        r = self.get(url, **params)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def catalog_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """For catalog paths that may or may not exist: None on 404 (try another path); NotCatalog on a 200
        that isn't JSON; FetchError on any other status, a network error or a robots block."""
        try:
            r = self.get(url, **params)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__} {url}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code} {url}")
        try:
            return r.json()
        except ValueError as e:
            raise NotCatalog(f"not JSON: {url}") from e

    def fetch_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """JSON body, or FetchError: for catalog pages, where a miss must never look like an empty catalog."""
        try:
            r = self.get(url, **params)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__} {url}") from e
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code} {url}")
        try:
            return r.json()
        except ValueError as e:
            raise FetchError(f"not JSON: {url}") from e
