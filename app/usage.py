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
instance's (CEILING_PER_MINUTE, DAILY_CAP); past the memory caps.

What it logs, so that "nobody came" and "everything was refused or lost" never look alike. Three lines, counts only
(never an event, an address, a prefix or a header), each answering one question:
- `usage start`, once per instance: did an instance start, and is it set up to publish?
- `usage flush`, one per flush window in which any request to /e arrived, all-zero windows included: how many
  requests came, how many were refused whole and why (REFUSED), how many events were accepted, how many were
  rejected and why (REJECTED), how many were published and how many still wait. A failed write or publish names its
  error's type in the same line.
- `usage receive failed`, at once and at ERROR, when the collector itself fails on a request: the error's type.
- `usage shutdown`, once per instance that is told to stop: what the last flush published, what was lost and why,
  and the instance's totals. A start line with no shutdown line after it is an instance that was killed without
  SIGTERM (out of memory, a crash): whatever it held is gone and nothing could say so.
A line is at WARNING when anything was dropped for a limit or a write or publish failed, and at ERROR when events
were lost; the usage alert (src/terraform/usage.tf) fires on either. Everything else is INFO.

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
# Counts that mean data was dropped by a limit or a publish failed (WARNING), or events are gone for good (ERROR:
# `lost`, and `error`, a request the collector itself failed on).
WARN = ("rate_limited", "ceiling", "daily_cap", "overflow", "staged_full", "publish_failed", "write_failed")
LOSS = ("lost", "error")
# Why a whole request was refused (its events are never read, so they can't be counted), and why one event was.
REFUSED = ("not_post", "aborted", "opted_out", "foreign_origin", "too_large", "bad_body", "error")
REJECTED = ("no_address", "invalid", "rate_limited", "ceiling", "daily_cap", "overflow", "staged_full")


def _level(counts: Mapping[str, int]) -> int:
    if any(counts.get(k) for k in LOSS):
        return logging.ERROR
    return logging.WARNING if any(counts.get(k) for k in WARN) else logging.INFO


def _reasons(counts: Mapping[str, int], names: tuple[str, ...]) -> str:
    """` (foreign_origin 2, bad_body 1)`: the reasons that happened, from fixed names only; empty if none did."""
    seen = [f"{k} {counts[k]}" for k in names if counts.get(k)]
    return f" ({', '.join(seen)})" if seen else ""


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
        self.counts: Counter[str] = Counter()  # this flush window's
        self.totals: Counter[str] = Counter()  # every flushed window's, for the shutdown line
        # Every accepted event is in exactly one place until it is published or reported lost: in memory, in a
        # flush's hands, or staged. These three, since start, say how many are still somewhere (`outstanding`).
        self.kept = 0  # accepted
        self.sent = 0  # published
        self.gone = 0  # reported lost by a flush line (a failed write with no room to put them back)
        self.started = clock()
        self.staged = 0  # events written to staging since the last successful publish
        self.last_flush = clock()
        self._lock = threading.Lock()  # the buffer, the counts, `staged`
        self._flushing = threading.Lock()  # one flush at a time

    # --- receiving ---------------------------------------------------------------------------------

    def receive(
        self, headers: Mapping[str, str], peer: str | None, body: bytes, oversize: bool = False
    ) -> None:
        """Take one request's events (headers lower-cased). Never raises: the reply is 204 whatever happens. The
        request and everything that became of it are counted in one step, so no flush line holds half a request."""
        seen: Counter[str] = Counter(requests=1)
        rows: list[dict[str, Any]] = []
        now = self.clock()
        try:
            rows = self._read(headers, peer, body, oversize, now, seen)
        except Exception as e:  # noqa: BLE001 - never fail a page
            seen, rows = Counter(requests=1, error=1), []
            # At once, not with the window's line: an instance killed before its next flush would never say so. The
            # type only (a message could quote the body); counts and the type are all the line holds.
            log.error(
                "usage receive failed: %s (1 request dropped by the collector itself)",
                type(e).__name__,
                extra={"fields": {"error": 1, "receive_error": type(e).__name__}},
            )
        with self._lock:
            memory = BUFFER_MAX - len(self.buffer)
            staging = STAGED_MAX - self.staged - len(self.buffer)
            room = max(0, min(memory, staging))
            if len(rows) > room:
                seen["overflow" if memory <= staging else "staged_full"] += len(rows) - room
                rows = rows[:room]
            if rows and not self.buffer:
                self.oldest = now
            self.buffer += rows
            seen["accepted"] += len(rows)
            self.kept += len(rows)
            self.counts.update(+seen)

    def refuse(self, reason: str) -> None:
        """A request to /e that never reaches `receive` (the client went away, or it wasn't a POST): counted as a
        request and under its reason, so the window it arrived in logs its line."""
        with self._lock:
            self.counts["requests"] += 1
            self.counts[reason] += 1

    def _read(
        self,
        headers: Mapping[str, str],
        peer: str | None,
        body: bytes,
        oversize: bool,
        now: float,
        seen: Counter[str],
    ) -> list[dict[str, Any]]:
        """The rows of one request's accepted events; why the request or any event was dropped goes in `seen`."""

        def refused(reason: str, n: int = 1) -> list[dict[str, Any]]:
            seen[reason] += n
            return []

        if headers.get("sec-gpc", "").strip() == "1" or headers.get("dnt", "").strip() == "1":
            return refused("opted_out")
        if headers.get("origin") not in self.origins:
            return refused("foreign_origin")
        if oversize or len(body) > MAX_BODY:
            return refused("too_large")
        try:
            doc = json.loads(body)
        except ValueError:
            return refused("bad_body")
        events = doc.get("events") if isinstance(doc, dict) and set(doc) == {"events"} else None
        if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
            return refused("bad_body")
        # The only place the full address exists: masked here and dropped with this frame.
        prefix = mask_ip(client_address(headers, peer, self.hops))
        if prefix is None:
            return refused("no_address", len(events))
        user_agent = headers.get("user-agent")
        at = datetime.fromtimestamp(now, UTC)
        rows = []
        for raw in events:
            try:
                e = Event.model_validate(raw)
            except ValidationError:
                seen["invalid"] += 1
                continue
            reason = self._limited(prefix, now)
            if reason:
                seen[reason] += 1
                continue
            rows.append(row(e, prefix, is_bot(user_agent, e.webdriver), at))
        return rows

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
        flush whose publish failed, or only counts to log and none logged for FLUSH_EVERY. Only a request (or the
        shutdown) asks, so a window's line waits for the next request after it is due, or for SIGTERM."""
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
        next due flush retries). Returns the events written. Logs one line of counts if any request arrived since
        the last one; never an event, a prefix or a header. With `wait`, gives up (returning -1) if another flush
        holds the lock that long."""
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
            self.totals.update(counts)
            self.last_flush = self.clock()
        written = files = 0
        published = None
        failed: dict[str, str] = {}  # write_error, publish_error: the exception's type, never its message

        def tally(key: str, n: int = 1) -> None:  # what the flush itself adds to the window's counts
            if not n:
                return
            counts[key] += n
            with self._lock:
                self.totals[key] += n
                if key == "published":
                    self.sent += n
                elif key == "lost":
                    self.gone += n

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
                tally("write_failed")
                failed["write_error"] = type(e).__name__
                tally("lost", self._requeue([r for _, later in days[i:] for r in later]))
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
                    sent, self.staged = self.staged, 0
                tally("published", sent)
                published = True
            except Exception as e:  # noqa: BLE001 - staged files stay for the next flush
                tally("publish_failed")
                failed["publish_error"] = type(e).__name__
                published = False
        self._log(counts, written, files, published, failed)
        return written

    def _requeue(self, rows: list[dict[str, Any]]) -> int:
        """Put rows a write refused back in memory; returns how many didn't fit (lost)."""
        with self._lock:
            room = max(0, BUFFER_MAX - len(self.buffer))
            self.buffer = rows[:room] + self.buffer
            if self.buffer and self.oldest is None:
                self.oldest = self.clock()
            return max(0, len(rows) - room)

    def _log(
        self,
        counts: Counter[str],
        written: int,
        files: int,
        published: bool | None,
        failed: Mapping[str, str] | None = None,
    ) -> None:
        """The window's one line. Nothing to say only when no request arrived and nothing was written or lost."""
        counts = +counts  # without any zeros
        if not (written or counts):
            return
        failed = failed or {}
        with self._lock:
            staged = self.staged
            waiting = max(
                0, self.kept - self.sent - self.gone
            )  # in memory or staged: accepted, not yet published
        refused = sum(counts.get(k, 0) for k in REFUSED)
        rejected = sum(counts.get(k, 0) for k in REJECTED)
        trouble = [
            f"{label} ({failed[key]})"
            for key, label in (
                ("write_error", "write failed"),
                ("publish_error", "publish failed"),
            )
            if key in failed
        ] + ([f"{counts['lost']} lost"] if counts.get("lost") else [])
        log.log(
            _level(counts),
            "usage flush: %d requests, %d refused%s; %d events accepted, %d rejected%s, %d published, %d waiting%s",
            counts.get("requests", 0),
            refused,
            _reasons(counts, REFUSED),
            counts.get("accepted", 0),
            rejected,
            _reasons(counts, REJECTED),
            counts.get("published", 0),
            waiting,
            "".join(f"; {t}" for t in trouble),
            extra={
                "fields": {
                    # Every window carries the same six, zeros included, so a query never meets a missing key.
                    "requests": 0,
                    "accepted": 0,
                    "published": 0,
                    **counts,
                    "refused": refused,
                    "rejected": rejected,
                    "waiting": waiting,
                    "written": written,
                    "files": files,
                    "published_ok": published,
                    "staged": staged,
                    **failed,
                }
            },
        )

    def start(self) -> None:
        """The instance's first line: that it started, and whether it is set up to publish. Booleans and counts only:
        never the dataset's name, an origin or the token."""
        target = os.environ.get("COFFEEDATA_USAGE") or ""
        remote = target.startswith("hf://")  # as Lake does
        token = bool(os.environ.get("HF_TOKEN"))
        log.info(
            "usage start: nothing waiting (an instance starts empty); %d origins allowed, %d proxy hops, "
            "publishes to %s%s",
            len(self.origins),
            self.hops,
            "a remote dataset" if remote else "a local folder",
            "" if not remote else ", token set" if token else ", NO token: nothing can be published",
            extra={
                "fields": {
                    "origins": len(self.origins),
                    "hops": self.hops,
                    "remote": remote,
                    "token": token,
                }
            },
        )

    def shutdown(self, budget: float = SHUTDOWN_BUDGET) -> None:
        """The last flush, within `budget` seconds: whatever isn't published by then dies with the instance. Always
        logs one line: what the last flush published, what was lost and why, and the instance's totals.

        Lost is every accepted event not published and not already reported lost (kept - sent - gone), wherever it
        is: in memory, staged, or in the hands of a flush that stalled opening the lake or writing. So an unfinished
        flush can never read as "0 lost". Any loss is at ERROR."""
        done = threading.Event()
        with self._lock:
            before = self.sent

        def last() -> None:
            if self.flush(wait=budget / 2) >= 0:
                done.set()

        worker = threading.Thread(target=last, name="usage-shutdown", daemon=True)
        worker.start()
        worker.join(budget)
        with self._lock:
            lost = max(0, self.kept - self.sent - self.gone)
            in_flight = max(
                0, lost - len(self.buffer) - self.staged
            )  # taken by a flush, neither staged nor back
            flushed = self.sent - before
            accepted, published = self.kept, self.sent
            totals = Counter(self.totals)
            if not done.is_set():  # a window no flush took: its counts still belong to the totals
                totals.update(self.counts)
        reason = "it ran out of time" if not done.is_set() else "the last publish or write failed"
        log.log(
            logging.ERROR if lost else logging.INFO,
            "usage shutdown: %d events published by the last flush, %d lost%s; since start: %d requests, "
            "%d events accepted, %d published",
            flushed,
            lost,
            f" ({reason})" if lost else "",
            totals.get("requests", 0),
            accepted,
            published,
            extra={
                "fields": {
                    "flushed": flushed,
                    "lost": lost,
                    "lost_in_flight": in_flight,
                    "finished": done.is_set(),
                    "total_requests": totals.get("requests", 0),
                    "total_accepted": accepted,
                    "total_published": published,
                    "uptime_s": round(self.clock() - self.started),
                }
            },
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
                    get().start()
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
        c = get()
        if method == "POST":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            declared = headers.get("content-length", "")
            cut: str | None  # why the body was cut short: too_large, aborted, or None (read whole)
            if declared.isdigit() and int(declared) > MAX_BODY:
                body, cut = b"", "too_large"  # not read at all
            else:
                body, cut = await _read_capped(receive, MAX_BODY)
            if cut == "aborted":
                c.refuse("aborted")
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
        else:
            c.refuse("not_post")
        return await _reply(
            send, 204
        )  # POST, and OPTIONS or anything else on /e: always 204, never a CORS header

    return app


app = create_app()

if __name__ == "__main__":
    sys.exit("run it with uvicorn: uvicorn app.usage:app --no-proxy-headers")
