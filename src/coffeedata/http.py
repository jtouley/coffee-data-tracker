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


class FetchError(Exception):
    """A catalog request failed (non-200, bad JSON, blocked). The roaster's fetch is recorded as an error."""


class Blocked(FetchError):
    """robots.txt disallows the URL."""


class RobotsUnavailable(FetchError, httpx.HTTPError):
    """robots.txt didn't answer (429, 430, 5xx), so nothing on that host is fetched for now. Callers treat it like
    a network error: this fetch fails and a later one asks again."""


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
            known = host in self._robots
        if not known:
            parser = urllib.robotparser.RobotFileParser()
            rules: urllib.robotparser.RobotFileParser | None = None  # None: no robots.txt, no restrictions
            try:
                r = self._send(f"https://{host}/robots.txt")
                if r.status_code == 200:
                    parser.parse(r.text.splitlines())
                    rules = parser
                elif r.status_code in (401, 403):
                    parser.disallow_all = True  # type: ignore[attr-defined]  # urllib.robotparser's own flag
                    rules = parser
                elif r.status_code in BLOCK_STATUSES or r.status_code >= 500:
                    # Not an answer about robots.txt, so nothing is fetched without it: this fetch fails (the
                    # retry pass tries again) and the host stays unknown, so the next call asks again.
                    raise RobotsUnavailable(
                        f"robots.txt for {host} answered {r.status_code}; not fetching without it"
                    )
            except RobotsUnavailable:
                raise
            except httpx.HTTPError:
                pass
            with self._lock:
                self._robots[host] = rules
        rp = self._robots[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def _wait_turn(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + self.min_interval
            self._last[host] = max(now, due)
        if due > now:
            time.sleep(due - now)

    def get(self, url: str, **params: str | int | float | bool | None) -> httpx.Response:
        if not self._allowed(url):
            raise Blocked(url)
        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self._wait_turn(host)
            retry_after = ""
            try:
                r = self._send(url, params or None)
            except httpx.HTTPError as e:
                last_exc = e
            else:
                if r.status_code not in RETRY_STATUSES:
                    return r
                last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                if self._limiter is not None and r.status_code in BLOCK_STATUSES:
                    continue  # the limiter's platform-wide cooldown is the wait; don't idle this worker too
                retry_after = r.headers.get("retry-after", "")
            if attempt < self.retries - 1:  # no pointless sleep after the final attempt
                time.sleep(min(int(retry_after), 30) if retry_after.isdigit() else 2**attempt)
        assert last_exc is not None
        raise last_exc

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
