"""A polite HTTP client: identifies itself, honors robots.txt, spaces requests per host, retries."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import urllib.robotparser
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from .text import scrub_surrogates

log = logging.getLogger(__name__)

USER_AGENT = "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker)"


class FetchError(Exception):
    """A catalog request failed (non-200, bad JSON, blocked). The roaster's fetch is recorded as an error."""


class Blocked(FetchError):
    """robots.txt disallows the URL."""


class NotCatalog(FetchError):
    """HTTP 200, but the body isn't the JSON a catalog endpoint returns (e.g. an HTML or challenge page)."""


@dataclass
class RequestStats:
    """What one tracked unit of work (a roaster's fetch) asked of the network: kept per thread by `track()`."""

    requests: int = 0  # every HTTP attempt, retries and robots.txt included
    pages: int = 0  # 200 responses other than robots.txt: listing pages, and any per-product or sitemap fetch
    retries: int = 0  # attempts after the first for one URL
    rate_limited: int = 0  # 429 responses
    statuses: Counter = field(default_factory=Counter)  # HTTP status -> responses; a network error has none

    def note(self, status: int | None, retry: bool = False, page: bool = True) -> None:
        self.requests += 1
        self.retries += retry
        if status is not None:
            self.statuses[status] += 1
            self.rate_limited += status == 429
            self.pages += page and status == 200


class PoliteClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        timeout: float = 15.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self.min_interval = min_interval
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
        self._local = threading.local()

    @contextlib.contextmanager
    def track(self) -> Iterator[RequestStats]:
        """Count this thread's requests until the block ends (the client is shared by every worker thread)."""
        prev, self._local.stats = getattr(self._local, "stats", None), RequestStats()
        try:
            yield self._local.stats
        finally:
            self._local.stats = prev

    def _note(self, status: int | None, retry: bool = False, page: bool = True) -> None:
        if (stats := getattr(self._local, "stats", None)) is not None:
            stats.note(status, retry, page)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        with self._lock:
            known = host in self._robots
        if not known:
            parser = urllib.robotparser.RobotFileParser()
            rules: urllib.robotparser.RobotFileParser | None = None  # None: no robots.txt, no restrictions
            try:
                r = self._client.get(f"https://{host}/robots.txt")
                self._note(r.status_code, page=False)
                if r.status_code == 200:
                    parser.parse(r.text.splitlines())
                    rules = parser
                elif r.status_code in (401, 403):
                    parser.disallow_all = True  # type: ignore[attr-defined]  # urllib.robotparser's own flag
                    rules = parser
            except httpx.HTTPError:
                self._note(None, page=False)
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
                r = self._client.get(url, params=params or None)
            except httpx.HTTPError as e:
                self._note(None, retry=attempt > 0)
                last_exc = e
            else:
                self._note(r.status_code, retry=attempt > 0)
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                retry_after = r.headers.get("retry-after", "")
            if attempt < self.retries - 1:  # no pointless sleep after the final attempt
                time.sleep(min(int(retry_after), 30) if retry_after.isdigit() else 2**attempt)
        assert last_exc is not None
        raise last_exc

    # Every JSON body is decoded with scrub_surrogates, so a store's lone surrogate never reaches a stored row.
    def get_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """JSON body on a 200 with a JSON payload, else None (for probing, where a miss is an answer)."""
        r = self.get(url, **params)
        if r.status_code != 200:
            return None
        try:
            return scrub_surrogates(r.json())
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
            return scrub_surrogates(r.json())
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
            return scrub_surrogates(r.json())
        except ValueError as e:
            raise FetchError(f"not JSON: {url}") from e
