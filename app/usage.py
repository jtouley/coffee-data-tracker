"""The usage collector: `POST /e`, its own small Cloud Run service (src/terraform/usage.tf), never the app. Run with
`COFFEEDATA_APP=app.usage` in the app's image, or `uvicorn app.usage:app`.

It never slows a page: the page's fetch is fire-and-forget (keepalive, no-cors), every POST gets an empty 204, and
nothing the collector does can turn that into an error. Accepted events wait in memory and go to the private usage
dataset (Lake.usage()), one file per day per flush.

When it flushes. Cloud Run gives the CPU only while a request is open (cpu_idle, the cheap setting), so a thread
working after the reply would crawl. So the flush runs in the request path: the request that finds a flush due (at
FLUSH_AT events or the oldest FLUSH_EVERY old, never sooner than PUBLISH_EVERY after the last) writes and publishes
before its own 204, on a worker thread so every other request is still answered at once. That one beacon waits
for the commit (about a second, at most FLUSH_WAIT); no page does. At SIGTERM (uvicorn's lifespan shutdown) it
flushes once more within SHUTDOWN_BUDGET of Cloud Run's 10 seconds, and counts as `lost` whatever doesn't make it.

Bounded: at most BUFFER_MAX events in memory and STAGED_MAX written but not yet published (the staging folder is on
the instance's in-memory filesystem); past either, new events are dropped and counted. A failed write puts its rows
back in memory. One commit a minute at most, so a flood can't make back-to-back commits.

Dropped before anything is kept, and counted (the counts are all it logs): Global Privacy Control or Do Not Track;
an Origin that isn't ours (a missing or "null" one too); a body over MAX_BODY or with more than MAX_EVENTS events;
an event the schema refuses (app/usage_events.py); over the limits per masked prefix (PER_MINUTE, PER_DAY) or the
instance's (CEILING_PER_MINUTE, DAILY_CAP); past the memory caps. A flush line logs at WARNING when anything was
dropped for a limit or a publish failed, and at ERROR when events were lost; the usage alert (alerts.tf) fires on
either.

The client's address is read from X-Forwarded-For the way the app does (limits.forwarded_client), masked at once
(IPv4 /24, IPv6 /40) and never kept, logged or used again: the limits count per masked prefix. Cookie,
Authorization and every other header are never read, except User-Agent, which only sets the bot flag.
"""

from __future__ import annotations

import asyncio
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
from .limits import FixedWindows, forwarded_client
from .usage_events import MAX_BODY, MAX_EVENTS, Event, is_bot, mask_ip, row

log = logging.getLogger("app.usage")

# Per masked prefix (a /24 or /40 can be a whole carrier's NAT pool or an iCloud Private Relay egress, so these are
# well above one person: a page load sends a few events, a busy session a few dozen a minute).
PER_MINUTE = 600
PER_DAY = 10_000
# Per instance (at most 2): a flood's ceiling, and the day's.
CEILING_PER_MINUTE = 6_000
DAILY_CAP = 200_000
FLUSH_AT = 200  # events in memory
FLUSH_EVERY = 15 * 60  # seconds since the oldest event in memory
PUBLISH_EVERY = 60  # seconds: at most one flush (and one commit) a minute per instance
FLUSH_WAIT = 20.0  # seconds the flushing request waits before replying anyway (usage.tf's timeout is 30 s)
BUFFER_MAX = 20_000  # events in memory
STAGED_MAX = 50_000  # events written to staging but not yet published
SHUTDOWN_BUDGET = 8.0  # seconds, of Cloud Run's 10 after SIGTERM
DEFAULT_ORIGINS = "https://registry.coffee,https://www.registry.coffee,https://app.registry.coffee"
# Counts that mean data was dropped by a limit or a publish failed (WARNING), or events are gone for good (ERROR).
WARN = ("rate_limited", "ceiling", "daily_cap", "overflow", "staged_full", "publish_failed", "write_failed")
LOSS = ("lost",)


def client_address(headers: Mapping[str, str], peer: str | None, hops: int) -> str:
    return forwarded_client(headers.get("x-forwarded-for", ""), peer, hops)


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
        self.limits = FixedWindows()
        self.buffer: list[dict[str, Any]] = []
        self.oldest: float | None = None
        self.counts: Counter[str] = Counter()
        self.staged = 0  # events written to staging since the last successful publish
        self.last_flush = clock()
        self._lock = threading.Lock()  # the buffer, the counts, `staged`
        self._flushing = threading.Lock()  # one flush at a time

    # --- receiving ---------------------------------------------------------------------------------

    def receive(
        self, headers: Mapping[str, str], peer: str | None, body: bytes, oversize: bool = False
    ) -> None:
        """Take one request's events (headers lower-cased). Never raises: the reply is 204 whatever happens."""
        try:
            self._receive(headers, peer, body, oversize)
        except Exception as e:  # noqa: BLE001 - never fail a page; the type only (a message could quote the body)
            self.count("error")
            log.error("usage receive failed: %s", type(e).__name__)

    def _receive(self, headers: Mapping[str, str], peer: str | None, body: bytes, oversize: bool) -> None:
        if headers.get("sec-gpc", "").strip() == "1" or headers.get("dnt", "").strip() == "1":
            return self.count("opted_out")
        if headers.get("origin") not in self.origins:
            return self.count("foreign_origin")
        if oversize or len(body) > MAX_BODY:
            return self.count("too_large")
        try:
            doc = json.loads(body)
        except ValueError:
            return self.count("bad_body")
        events = doc.get("events") if isinstance(doc, dict) and set(doc) == {"events"} else None
        if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
            return self.count("bad_body")
        # The only place the full address exists: masked here and dropped with this frame.
        prefix = mask_ip(client_address(headers, peer, self.hops))
        if prefix is None:
            return self.count("no_address", len(events))
        user_agent = headers.get("user-agent")
        now = self.clock()
        at = datetime.fromtimestamp(now, UTC)
        rows = []
        for raw in events:
            try:
                e = Event.model_validate(raw)
            except ValidationError:
                self.count("invalid")
                continue
            reason = self._limited(prefix, now)
            if reason:
                self.count(reason)
                continue
            rows.append(row(e, prefix, is_bot(user_agent, e.webdriver), at))
        with self._lock:
            memory = BUFFER_MAX - len(self.buffer)
            staging = STAGED_MAX - self.staged - len(self.buffer)
            room = max(0, min(memory, staging))
            if len(rows) > room:
                self.counts["overflow" if memory <= staging else "staged_full"] += len(rows) - room
                rows = rows[:room]
            if rows and not self.buffer:
                self.oldest = now
            self.buffer += rows
            if rows:
                self.counts["accepted"] += len(rows)

    def _limited(self, prefix: str, now: float) -> str | None:
        """Why this event is over a limit, or None. An event is counted against every limit only when it passes them
        all, so a refused one uses up nothing."""
        key = self.limits.blocked(
            {
                "instance-day": (DAILY_CAP, 86400.0),
                "instance-minute": (CEILING_PER_MINUTE, 60.0),
                f"m|{prefix}": (PER_MINUTE, 60.0),
                f"d|{prefix}": (PER_DAY, 86400.0),
            },
            now,
        )
        return {None: None, "instance-day": "daily_cap", "instance-minute": "ceiling"}.get(
            key, "rate_limited"
        )

    def count(self, reason: str, n: int = 1) -> None:
        with self._lock:
            self.counts[reason] += n

    # --- flushing ----------------------------------------------------------------------------------

    def due(self) -> bool:
        """At most once every PUBLISH_EVERY: FLUSH_AT events waiting, the oldest FLUSH_EVERY old, files staged by a
        flush whose publish failed, or only counts to log and none logged for FLUSH_EVERY."""
        with self._lock:
            now = self.clock()
            if now - self.last_flush < PUBLISH_EVERY:
                return False
            if self.buffer:
                return len(self.buffer) >= FLUSH_AT or now - (self.oldest or now) >= FLUSH_EVERY
            return self.staged > 0 or (bool(self.counts) and now - self.last_flush >= FLUSH_EVERY)

    def lake(self) -> Lake:
        if self._lake is None:
            self._lake = self._lake_factory()
        return self._lake

    def flush(self, wait: float | None = None) -> int:
        """Write what's in memory, one file per day, then publish everything staged in one commit (one attempt: the
        next due flush retries). Returns the events written. Logs one line of counts; never an event, a prefix or a
        header. With `wait`, gives up (returning -1) if another flush holds the lock that long."""
        if not self._flushing.acquire(timeout=-1 if wait is None else wait):
            return -1
        try:
            return self._flush()
        finally:
            self._flushing.release()

    def _flush(self) -> int:
        with self._lock:
            rows, self.buffer, self.oldest = self.buffer, [], None
            counts, self.counts = self.counts, Counter()
            self.last_flush = self.clock()
        written = files = 0
        published = None
        lake = None
        by_day: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_day.setdefault(r["received_at"].strftime("%Y-%m-%d"), []).append(r)
        days = sorted(by_day.items())
        for i, (day, day_rows) in enumerate(days):
            try:
                lake = lake or self.lake()
                lake.write("usage_events", f"events/day={day}/batch={new_batch_id()}.parquet", day_rows)
            except Exception as e:  # noqa: BLE001 - this day's rows and the later days' go back in memory
                counts["write_failed"] += 1
                self._requeue([r for _, later in days[i:] for r in later], counts)
                log.warning("usage write failed: %s", type(e).__name__)
                break
            written += len(day_rows)
            files += 1
            with self._lock:
                self.staged += len(day_rows)
        if lake is None and self.staged:
            lake = self.lake()
        if lake is not None and self.staged:
            try:
                lake.publish(f"usage: {self.staged} events", attempts=1)
                with self._lock:
                    counts["published"] += self.staged
                    self.staged = 0
                published = True
            except Exception as e:  # noqa: BLE001 - staged files stay for the next flush
                counts["publish_failed"] += 1
                published = False
                log.warning("usage publish failed: %s", type(e).__name__)
        self._log(counts, written, files, published)
        return written

    def _requeue(self, rows: list[dict[str, Any]], counts: Counter[str]) -> None:
        with self._lock:
            room = max(0, BUFFER_MAX - len(self.buffer))
            self.buffer = rows[:room] + self.buffer
            if self.buffer and self.oldest is None:
                self.oldest = self.clock()
            if len(rows) > room:
                counts["lost"] += len(rows) - room

    def _log(self, counts: Counter[str], written: int, files: int, published: bool | None) -> None:
        if not (written or counts):
            return
        level = (
            logging.ERROR
            if any(counts.get(k) for k in LOSS)
            else logging.WARNING
            if any(counts.get(k) for k in WARN)
            else logging.INFO
        )
        log.log(
            level,
            "usage flush: %d events in %d files",
            written,
            files,
            extra={
                "fields": {
                    "written": written,
                    "files": files,
                    "published_ok": published,
                    "staged": self.staged,
                    **counts,
                }
            },  # fmt: skip
        )

    def shutdown(self, budget: float = SHUTDOWN_BUDGET) -> None:
        """The last flush, within `budget` seconds: whatever isn't published by then dies with the instance, and is
        logged at ERROR as `lost` (the events in memory and those staged but not published)."""
        done = threading.Event()

        def last() -> None:
            if self.flush(wait=budget / 2) >= 0:
                done.set()

        worker = threading.Thread(target=last, name="usage-shutdown", daemon=True)
        worker.start()
        worker.join(budget)
        with self._lock:
            lost = len(self.buffer) + self.staged
        if lost:
            reason = "it ran out of time" if not done.is_set() else "the last publish failed"
            log.error(
                "usage shutdown: %d events lost (%s)",
                lost,
                reason,
                extra={"fields": {"lost": lost, "finished": done.is_set()}},
            )


# --- the ASGI app ------------------------------------------------------------------------------------------------

_HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]  # never Set-Cookie


async def _reply(send: Callable, status: int, body: bytes = b"") -> None:
    headers = [*_HEADERS, (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _read_capped(receive: Callable, limit: int) -> tuple[bytes, str | None]:
    """The body, read up to `limit` + 1 bytes, and why it was cut short: `too_large` (the rest is never read) or
    `aborted` (the client went away)."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"", "aborted"
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return b"", "too_large"
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks), None


def create_app(collector: Collector | None = None) -> Callable:
    """A bare ASGI app, so nothing (routing, middleware, a framework's error page) stands between a request and its
    204. The collector is made at startup unless one is given (tests)."""
    state: dict[str, Any] = {"collector": collector}

    def get() -> Collector:
        if state["collector"] is None:  # no lifespan (a bare server): make it now
            state["collector"] = Collector()
        return state["collector"]

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    if os.environ.get("K_SERVICE") or os.environ.get("COFFEEDATA_JSON_LOGS"):
                        logs.setup()
                    logging.getLogger("uvicorn.access").disabled = True  # its line holds the client's address
                    get()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await asyncio.to_thread(get().shutdown)
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
            c = get()
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            declared = headers.get("content-length", "")
            cut: str | None  # why the body was cut short: too_large, aborted, or None (read whole)
            if declared.isdigit() and int(declared) > MAX_BODY:
                body, cut = b"", "too_large"  # not read at all
            else:
                body, cut = await _read_capped(receive, MAX_BODY)
            if cut == "aborted":
                c.count("aborted")
                return
            peer = scope.get("client")
            c.receive(headers, peer[0] if peer else None, body, cut == "too_large")
            if c.due():
                # While this request is open the instance has its CPU. The flush runs on its own thread, so every
                # other request is answered meanwhile; this one waits for it at most FLUSH_WAIT, then replies anyway.
                worker = threading.Thread(target=c.flush, args=(0.0,), name="usage-flush", daemon=True)
                worker.start()
                deadline = time.monotonic() + FLUSH_WAIT
                while worker.is_alive() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
        return await _reply(
            send, 204
        )  # POST, and OPTIONS or anything else on /e: always 204, never a CORS header

    return app


app = create_app()

if __name__ == "__main__":
    sys.exit("run it with uvicorn: uvicorn app.usage:app --no-proxy-headers")
