"""The usage collector: `POST /e` on e.registry.coffee, its own small Cloud Run service (src/terraform/usage.tf), never
the app. Run with `COFFEEDATA_APP=app.usage` in the app's image, or `uvicorn app.usage:app`.

It never blocks or slows a page: every POST gets an empty 204 straight away, whatever happened to it, and nothing
it does waits on the network before replying. Accepted events wait in memory and go to the private usage dataset
(Lake.usage()) in one file per day per flush, at FLUSH_AT events, every FLUSH_EVERY seconds, or at shutdown
(SIGTERM, through uvicorn's lifespan). A flush that fails to publish leaves its files staged for the next one.

Dropped before anything is kept, and counted (the counts are all it logs): Global Privacy Control or Do Not Track;
an Origin that isn't ours (a missing or "null" one too); a body over MAX_BODY or with more than MAX_EVENTS events;
an event the schema refuses (app/usage_events.py); over the per-prefix limits (PER_MINUTE, PER_DAY) or the
service's ceiling (CEILING_PER_MINUTE); past the memory cap (BUFFER_MAX).

The client's address is read from X-Forwarded-For the way the app does (COFFEEDATA_PROXY_HOPS from the right),
masked at once (IPv4 /24, IPv6 /40) and never kept, logged or used again: the rate limits count per masked prefix.
Cookie, Authorization and every other header are never read, except User-Agent, which only sets the bot flag.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from coffeedata.lake import Lake, new_batch_id

from . import logs
from .limits import Windows
from .usage_events import Event, is_bot, mask_ip, row

log = logging.getLogger("app.usage")

MAX_BODY = 4096  # bytes
MAX_EVENTS = 20  # per request
PER_MINUTE = 60  # events per masked prefix
PER_DAY = 2000
CEILING_PER_MINUTE = 6000  # every prefix together
FLUSH_AT = 200  # events in memory
FLUSH_EVERY = 15 * 60  # seconds since the oldest event in memory
BUFFER_MAX = 20_000  # events in memory while publishing is failing; past it, new ones are dropped
SHUTDOWN_ATTEMPTS = 2  # Cloud Run allows 10 s after SIGTERM: no long backoff then
DEFAULT_ORIGINS = "https://registry.coffee,https://www.registry.coffee,https://app.registry.coffee"


def client_address(headers: Mapping[str, str], peer: str | None, hops: int) -> str:
    """The caller's address: behind `hops` proxies that each append to X-Forwarded-For, that many entries from the
    right (anything further left came from the client and can't be trusted); else the socket's peer (app/web.py's
    client_ip, on plain headers)."""
    if hops > 0:
        chain = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return peer or ""


class Collector:
    """Validates, masks and buffers events; flushes them to the usage lake. `receive` never raises."""

    def __init__(
        self,
        lake: Callable[[], Lake] = Lake.usage,
        origins: frozenset[str] | None = None,
        hops: int | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._lake_factory = lake
        self._lake: Lake | None = None
        env_origins = os.environ.get("COFFEEDATA_USAGE_ORIGINS") or DEFAULT_ORIGINS
        self.origins = origins or frozenset(o.strip() for o in env_origins.split(",") if o.strip())
        self.hops = int(os.environ.get("COFFEEDATA_PROXY_HOPS") or 0) if hops is None else hops
        self.clock = clock
        self.limits = Windows()
        self.buffer: list[dict[str, Any]] = []
        self.oldest: float | None = None
        self.counts: Counter[str] = Counter()
        self.unpublished = False  # files staged by a flush whose publish failed
        self.last_flush = clock()
        self._lock = threading.Lock()  # the buffer and the counts
        self._flushing = threading.Lock()  # one flush at a time
        self._worker: threading.Thread | None = None

    # --- receiving ---------------------------------------------------------------------------------

    def receive(
        self, headers: Mapping[str, str], peer: str | None, body: bytes, oversize: bool = False
    ) -> None:
        """Take one request's events (headers lower-cased). Never raises: the reply is 204 whatever happens."""
        try:
            self._receive(headers, peer, body, oversize)
        except Exception as e:  # noqa: BLE001 - never fail a page; the type only (a message could quote the body)
            self._count("error")
            log.error("usage receive failed: %s", type(e).__name__)
        self.flush_if_due()

    def _receive(self, headers: Mapping[str, str], peer: str | None, body: bytes, oversize: bool) -> None:
        if headers.get("sec-gpc", "").strip() == "1" or headers.get("dnt", "").strip() == "1":
            return self._count("opted_out")
        if headers.get("origin") not in self.origins:
            return self._count("foreign_origin")
        if oversize or len(body) > MAX_BODY:
            return self._count("too_large")
        try:
            doc = json.loads(body)
        except ValueError:
            return self._count("bad_body")
        events = doc.get("events") if isinstance(doc, dict) and set(doc) == {"events"} else None
        if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
            return self._count("bad_body")
        # The only place the full address exists: masked here and dropped with this frame.
        prefix = mask_ip(client_address(headers, peer, self.hops))
        if prefix is None:
            return self._count("no_address", len(events))
        user_agent = headers.get("user-agent")
        now = self.clock()
        at = datetime.fromtimestamp(now, UTC)
        rows = []
        for raw in events:
            try:
                e = Event.model_validate(raw)
            except ValidationError:
                self._count("invalid")
                continue
            limits = {
                f"m|{prefix}": (PER_MINUTE, 60.0),
                f"d|{prefix}": (PER_DAY, 86400.0),
                "ceiling": (CEILING_PER_MINUTE, 60.0),
            }
            if not self.limits.take(limits, now):
                self._count("rate_limited")
                continue
            rows.append(row(e, prefix, is_bot(user_agent, e.webdriver), at))
        with self._lock:
            room = max(0, BUFFER_MAX - len(self.buffer))
            if len(rows) > room:
                self.counts["overflow"] += len(rows) - room
                rows = rows[:room]
            if rows and not self.buffer:
                self.oldest = now
            self.buffer += rows
            self.counts["accepted"] += len(rows)

    def _count(self, reason: str, n: int = 1) -> None:
        with self._lock:
            self.counts[reason] += n

    # --- flushing ----------------------------------------------------------------------------------

    def due(self) -> bool:
        """FLUSH_AT events waiting, the oldest FLUSH_EVERY old, or only counts to log and none logged for as long."""
        with self._lock:
            now = self.clock()
            if self.buffer:
                return len(self.buffer) >= FLUSH_AT or now - (self.oldest or now) >= FLUSH_EVERY
            return bool(self.counts) and now - self.last_flush >= FLUSH_EVERY

    def flush_if_due(self) -> None:
        """Start a flush in the background when one is due and none is running; never waits for it."""
        if not self.due() or (self._worker is not None and self._worker.is_alive()):
            return
        self._worker = threading.Thread(target=self.flush, name="usage-flush", daemon=True)
        self._worker.start()

    def lake(self) -> Lake:
        if self._lake is None:
            self._lake = self._lake_factory()
        return self._lake

    def flush(self, attempts: int = 5) -> int:
        """Write what's in memory, one file per day, and publish it with any files an earlier flush left staged.
        Returns the events written. Logs one line of counts; never an event, a prefix or a header."""
        with self._flushing:
            with self._lock:
                rows, self.buffer, self.oldest = self.buffer, [], None
                counts, self.counts = self.counts, Counter()
                self.last_flush = self.clock()
            written = files = 0
            published = None
            try:
                lake = self.lake()
                by_day: dict[str, list[dict[str, Any]]] = {}
                for r in rows:
                    by_day.setdefault(r["received_at"].strftime("%Y-%m-%d"), []).append(r)
                for day, day_rows in sorted(by_day.items()):
                    lake.write("usage_events", f"events/day={day}/batch={new_batch_id()}.parquet", day_rows)
                    written += len(day_rows)
                    files += 1
                if files or self.unpublished:
                    self.unpublished = True
                    lake.publish(f"usage: {written} events", attempts=attempts)
                    self.unpublished = False
                    published = True
            except Exception as e:  # noqa: BLE001 - kept staged (or lost, if the write failed); counted and logged
                published = False
                counts["write_failed" if written < len(rows) else "publish_failed"] += 1
                if written < len(rows):
                    counts["lost"] += len(rows) - written
                log.warning("usage flush failed: %s", type(e).__name__)
            if rows or counts:
                log.info(
                    "usage flush: %d events in %d files",
                    written,
                    files,
                    extra={"fields": {"written": written, "files": files, "published": published, **counts}},
                )
            return written

    def start_timer(self, every: float = 60.0) -> threading.Event:
        """Check every `every` seconds whether a flush is due (Cloud Run gives CPU between requests only with
        instance-based billing, so this may sleep through idle time: the request path and shutdown check too)."""
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(every):
                self.flush_if_due()

        threading.Thread(target=run, name="usage-timer", daemon=True).start()
        return stop


# --- the ASGI app ------------------------------------------------------------------------------------------------

_HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]  # never Set-Cookie


async def _reply(send: Callable, status: int, body: bytes = b"") -> None:
    headers = [*_HEADERS, (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _read_capped(receive: Callable, limit: int) -> tuple[bytes, bool]:
    """The body, read up to `limit` + 1 bytes; whether it was longer (the rest is never read)."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"", True  # the client went away
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks), False


def create_app(collector: Collector | None = None) -> Callable:
    """A bare ASGI app, so nothing (routing, middleware, a framework's error page) stands between a request and its
    204. The collector is made at startup unless one is given (tests)."""
    state: dict[str, Any] = {"collector": collector, "stop": None}

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    if os.environ.get("K_SERVICE") or os.environ.get("COFFEEDATA_JSON_LOGS"):
                        logs.setup()
                    logging.getLogger("uvicorn.access").disabled = True  # its line holds the client's address
                    state["collector"] = state["collector"] or Collector()
                    state["stop"] = state["collector"].start_timer()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if state["stop"] is not None:
                        state["stop"].set()
                    if state["collector"] is not None:
                        state["collector"].flush(attempts=SHUTDOWN_ATTEMPTS)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        method, path = scope["method"], scope["path"]
        # /healthz for the startup probe (it calls the container directly); /health for an uptime check through
        # Google's front end, which answers paths ending in "z" itself (as the app, app/main.py).
        if path in ("/health", "/healthz") and method in ("GET", "HEAD"):
            return await _reply(send, 200, b"ok")
        if path != "/e":
            return await _reply(send, 404)
        if method == "POST":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BODY:
                body, oversize = b"", True  # not read at all
            else:
                body, oversize = await _read_capped(receive, MAX_BODY)
            if state["collector"] is None:  # no lifespan (a bare server): make it now
                state["collector"] = Collector()
            peer = scope.get("client")
            state["collector"].receive(headers, peer[0] if peer else None, body, oversize)
        return await _reply(
            send, 204
        )  # POST, and OPTIONS or anything else on /e: always 204, never a CORS header

    return app


app = create_app()

if __name__ == "__main__":
    sys.exit("run it with uvicorn: uvicorn app.usage:app --no-proxy-headers")
