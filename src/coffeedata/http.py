"""A polite HTTP client: identifies itself, honors robots.txt, spaces requests per host, retries."""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker)"


class Blocked(Exception):
    """robots.txt disallows the URL."""


class PoliteClient:
    def __init__(self, min_interval: float = 1.0, timeout: float = 15.0, retries: int = 3,
                 transport: httpx.BaseTransport | None = None):
        self.min_interval = min_interval
        self.retries = retries
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=timeout, follow_redirects=True, transport=transport,
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

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        with self._lock:
            known = host in self._robots
        if not known:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                r = self._client.get(f"https://{host}/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt (or unreadable) means no restrictions
            except httpx.HTTPError:
                rp = None
            with self._lock:
                self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def _wait_turn(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + self.min_interval
            self._last[host] = max(now, due)
        if due > now:
            time.sleep(due - now)

    def get(self, url: str, **params: object) -> httpx.Response:
        if not self._allowed(url):
            raise Blocked(url)
        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self._wait_turn(host)
            try:
                r = self._client.get(url, params=params or None)
            except httpx.HTTPError as e:
                last_exc = e
            else:
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                retry_after = r.headers.get("retry-after", "")
                if retry_after.isdigit():
                    time.sleep(min(int(retry_after), 30))
            time.sleep(2**attempt)
        assert last_exc is not None
        raise last_exc

    def get_json(self, url: str, **params: object) -> object | None:
        """JSON body on a 200 with a JSON payload, else None."""
        r = self.get(url, **params)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
