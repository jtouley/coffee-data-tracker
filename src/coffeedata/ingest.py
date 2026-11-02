"""Weekly pipeline: fetch every roaster with a usable storefront, append the raw results to the lake.

Writes (never rewrites): a run manifest (the roasters this ingest sets out to fetch, before the first request),
snapshots (every product, coffee or not), fetches (one row per roaster attempt, ok or error, with its request
diagnostics) and bodies (description text, once per new body_hash). Extract and load only (ELT): nothing is
classified or extracted here. The build (stg_products) decides what is a coffee and its rule attributes, and the
post-load enrichment job (enrich.py) picks Claude's candidates from the raw rows. Re-running for the same run
date only fetches roasters without a successful fetch that day.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import time
from collections import Counter, deque
from collections.abc import Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_any
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from . import crashguard, lineage, registry
from .adapters import fetch_catalog
from .adapters.shopify import PageBlocked, StoreBlocked
from .http import (
    BLOCK_STATUSES,
    CHALLENGE_STATUSES,
    MAX_STORE_WAIT,
    SERVER_RETRY_STATUSES,
    FetchError,
    PoliteClient,
    RequestStats,
    RobotsUnavailable,
    Unavailable,
    egress_ip,
    retry_after_seconds,
    server_pause,
    store_wait,
)
from .lake import Lake, new_batch_id, retry_transient
from .models import CatalogItem, Roaster
from .ratelimit import SHOPIFY_CONCURRENCY, SHOPIFY_RPS, HostRateLimited, PlatformLimiter, RateLimited

log = logging.getLogger(__name__)

INGESTABLE = {"pending", "active", "no_coffee", "no_catalog"}
CHUNK = 200  # roasters per written and published batch: a crash loses at most one chunk
DEFERRED_ROUNDS = 2  # after the main pass, rate-limited or timed-out roasters are retried this many times
# A deferred round's retries are spread over this many seconds (a Retry-After is typically 60), so parked stores
# don't all wake and ask at the same instant.
DEFERRED_SPREAD = 60.0
# A store deferred on a block (429, 430, 503, or a 500/502/504 with a long Retry-After) is asked again no sooner than
# its Retry-After or these many seconds after its failure (one per deferred round), whichever is longer, plus its place
# in the round's spread. Never later than http.MAX_STORE_WAIT (5 minutes): a store asking for longer is asked again
# then, in the next deferred round, and never scheduled past the run's deadline.
DEFERRED_BACKOFFS = (60.0, 120.0)
# A store that pauses us on a catalog page (shopify.PageBlocked) is asked for that same page again after each of
# these waits (at least its Retry-After, at most http.MAX_STORE_WAIT), without holding a worker meanwhile; the pages it
# already served are kept. A backoff that would end past the run's deadline parks the store at once.
# After the last one it is parked, with its progress, for `coffeedata crawl-retry`.
PAGE_BACKOFFS = (60.0, 120.0, 240.0)


def _causes(e: BaseException | None) -> Iterator[BaseException]:
    while e is not None:
        yield e
        e = e.__cause__


def challenged(e: BaseException) -> bool:
    """An edge answered with a challenge instead of the catalog (http.CHALLENGE_STATUSES: SiteGround's 202) or its
    robots.txt (http.is_challenge: a 202, or a challenge header on any status)."""
    return any(
        (isinstance(c, FetchError) and c.status in CHALLENGE_STATUSES)
        or (isinstance(c, RobotsUnavailable) and c.challenge)
        for c in _causes(e)
    )


def blocked(e: BaseException) -> bool:
    """The store (or the platform) refused with a block status: 429, 430 or 503, an edge's challenge (202), or the
    limiter's own refusal. The status may come wrapped (an HTTPStatusError cause) or bare (a FetchError from
    fetch_json or catalog_json)."""
    return challenged(e) or any(
        isinstance(c, RateLimited)
        or (isinstance(c, httpx.HTTPStatusError) and c.response.status_code in BLOCK_STATUSES)
        or (isinstance(c, FetchError) and c.status in BLOCK_STATUSES)
        for c in _causes(e)
    )


def server_paused(e: BaseException) -> float | None:
    """How long a 500, 502 or 504 asked us to stay away, when it asked for longer than the client waits
    (http.server_pause, at most MAX_STORE_WAIT), on the catalog or on robots.txt. None for any other failure."""
    for c in _causes(e):
        if isinstance(c, httpx.HTTPStatusError) and (wait := server_pause(c.response)) is not None:
            return wait
        if isinstance(c, RobotsUnavailable) and not c.challenge and c.status in SERVER_RETRY_STATUSES:
            return c.wait or 0.0
    return None


def deferrable(e: BaseException) -> bool:
    """A failure worth another try once the rest of the run is done: rate limiting (429, 430, 503, on any request,
    robots.txt included), a 500, 502 or 504 with a long Retry-After (server_paused) or a timeout. Never a refusal (401, 403: shopify.StoreBlocked; an edge's challenge, which
    answers every retry the same way: the 15:18 resume of 2026-11-02 asked 32 challenged stores again and got 32
    challenges), and never a page block, which ingest.run backs off itself."""
    if challenged(e) or any(isinstance(c, StoreBlocked | PageBlocked) for c in _causes(e)):
        return False
    return (
        blocked(e)
        or server_paused(e) is not None
        or any(isinstance(c, httpx.TimeoutException) for c in _causes(e))
    )


def block_wait(e: BaseException) -> float | None:
    """How long the store asked us to wait (its Retry-After, a 5xx's long one, or the limiter's pause for it), if it
    said: at most MAX_STORE_WAIT, however long it asked for."""
    for c in _causes(e):
        if isinstance(c, HostRateLimited | RobotsUnavailable) and c.wait is not None:
            return store_wait(c.wait)
        if isinstance(c, httpx.HTTPStatusError) and c.response.status_code in BLOCK_STATUSES:
            asked = retry_after_seconds(c.response.headers.get("retry-after"))
            return None if asked is None else store_wait(asked)
        if isinstance(c, httpx.HTTPStatusError) and (wait := server_pause(c.response)) is not None:
            return wait
    return None


def parked_path(lake: Lake, run_date: str, roaster_id: str) -> Path:
    """Where a parked store's progress (its blocked page and the products before it) waits for crawl-retry.
    Outside every lake table's glob: dbt never reads it."""
    return lake.root / "parked" / f"run_date={run_date}" / f"{roaster_id}.json"


def save_parked(lake: Lake, run_date: str, roaster_id: str, progress: dict) -> None:
    p = parked_path(lake, run_date, roaster_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # body_text is excluded from a CatalogItem's dump; the parked pages' descriptions are kept with them
    items = [i.model_dump(mode="json") | {"body_text": i.body_text} for i in progress["items"]]
    doc = {"page": progress["page"], "items": items}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(p)


def load_parked(lake: Lake, run_date: str) -> dict[str, dict]:
    """Every parked store of the run: roaster_id -> {"page", "items"}, ready to pass to run(resume=...)."""
    out = {}
    for p in sorted((lake.root / "parked" / f"run_date={run_date}").glob("*.json")):
        doc = json.loads(p.read_text())
        out[p.stem] = {"page": doc["page"], "items": [CatalogItem.model_validate(i) for i in doc["items"]]}
    return out


@dataclass
class Result:
    roaster: Roaster
    fetched_at: datetime
    items: list[CatalogItem] = field(default_factory=list)
    error: str | None = None
    stats: RequestStats = field(default_factory=RequestStats)
    duration_s: float | None = None
    deferrable: bool = False
    # a deferrable failure's Retry-After (block_wait): the store isn't asked again before it passes
    wait: float | None = None
    # failed on a block status (429, 430, 503), an edge's challenge (202) or the limiter's refusal
    blocked: bool = False
    unavailable: bool = False  # the store is closed, deactivated or password-protected (http.Unavailable)
    progress: dict | None = None  # blocked mid-catalog (shopify.PageBlocked): {"page", "items", "wait"}
    # not fetched: in flight in 2 dead attempts this week (crashguard); its fetches status is `crashed`
    quarantined: bool = False

    @property
    def status(self) -> str:
        """Its fetches status (lake.FETCH_STATUSES)."""
        if self.quarantined:
            return crashguard.CRASHED
        return "unavailable" if self.unavailable else "error" if self.error else "ok"


def ingest_one(
    client: PoliteClient, roaster: Roaster, resume: dict | None = None, known_products: int | None = None
) -> Result:
    res = Result(roaster, datetime.now(UTC).replace(microsecond=0))
    start = time.monotonic()
    with client.track() as res.stats:
        try:
            res.items = list(
                {i.product_id: i for i in fetch_catalog(client, roaster, resume, known_products)}.values()
            )  # feeds repeat items
        except PageBlocked as e:
            res.error = f"{type(e).__name__}: {e}"[:300]
            res.items = []  # never a partial catalog in the lake: the pages so far wait in `progress`
            res.blocked = True
            res.progress = {"page": e.page, "items": e.items, "wait": e.wait, "host": e.host}
        except Exception as e:  # noqa: BLE001 - any failure is this roaster's error, never the run's
            res.error = f"{type(e).__name__}: {e}"[:300]
            res.items = []
            res.deferrable = deferrable(e)
            res.wait = block_wait(e) if res.deferrable else None
            res.blocked = blocked(e)
            res.unavailable = any(isinstance(c, Unavailable) for c in _causes(e))
    res.duration_s = round(time.monotonic() - start, 3)
    return res


def _attempt(
    client: PoliteClient,
    roaster: Roaster,
    resume: dict | None,
    known: int | None,
    guard: crashguard.Guard | None = None,
    last: bool = False,
) -> Result:
    """One roaster's fetch, in a worker. Its in-flight marker (guard) is written when the worker starts it, not when
    it is queued (every roaster is queued at once), and removed when it returns: only the fetches actually running
    when the process dies are left marked. last: the store's last try in this pass (PoliteClient.last_try)."""
    if guard is not None:
        guard.start(roaster.roaster_id)
    try:
        with client.last_try(last):
            if resume is None and known is None:
                res = ingest_one(client, roaster)
            else:
                res = ingest_one(client, roaster, resume, known)
    except Exception:  # contained by run(): the fetch returned, with an error
        if guard is not None:
            guard.end(roaster.roaster_id)
        raise
    # A BaseException (SystemExit, an interpreter-level kill) leaves the marker, as an OOM kill or segfault does.
    if guard is not None:
        guard.end(roaster.roaster_id)
    return res


def known_products(lake: Lake) -> dict[str, int]:
    """Each roaster's product count at its latest complete ok fetch with products: lets a Shopify short page end a
    catalog that has reached its known size, and never one known to be bigger. A partial fetch (a catalog read only
    in part) is never the known size."""
    q = f"""select roaster_id, arg_max(products, fetched_at) from {lake.scan("fetches")}
            where status = 'ok' and products > 0 and not coalesce(partial, false) and roaster_id is not null
            group by 1"""
    return dict(retry_transient(lambda: lake.connect().execute(q).fetchall()))


def blocked_hosts(lake: Lake, run_date: str) -> set[str]:
    """The hosts that answered a block (fetches.blocks) earlier in this run: an earlier job, attempt or pass of the
    same run_date. Another block from one is a repeat, never that store's first block in the run."""
    q = f"""select distinct b.host from (select unnest(blocks) as b from {lake.scan("fetches")} where run_date = ?)
            where b.host is not null"""
    rows = retry_transient(lambda: lake.connect().execute(q, [date.fromisoformat(run_date)]).fetchall())
    return {r[0] for r in rows}


def sample_key(roaster_id: str) -> str:
    """Stable order for --sample: unaffected by roasters being added, shared by probe and ingest."""
    return hashlib.sha1(roaster_id.encode()).hexdigest()


def select(
    lake: Lake, run_date: str, only: str | None = None, sample: int | None = None, retry_failed: bool = False
) -> list[Roaster]:
    return [s.roaster for s in select_states(lake, run_date, only, sample, retry_failed)[0]]


def select_states(
    lake: Lake, run_date: str, only: str | None = None, sample: int | None = None, retry_failed: bool = False
) -> tuple[list[registry.RoasterState], str]:
    """`select`, with each roaster's status now and the run's mode (full, sample, only or retry_failed): what the
    run manifest records."""
    mode = "only" if only else "retry_failed" if retry_failed else "sample" if sample else "full"
    states = registry.current(lake)
    if only:
        return [s for s in states if s.roaster.roaster_id == only], mode
    done, tried = retry_transient(_done_and_tried, lake, run_date)
    # Draw a sample from a population that doesn't change as the run progresses, so re-running the same
    # command picks the same roasters (and then skips the ones already done).
    pool = sorted(
        (s for s in states if s.roaster.platform and s.status != "opted_out"),
        key=lambda s: s.roaster.roaster_id,
    )
    if sample:  # a hash order is stable as roasters are added, so weekly samples keep the same roasters
        pool = sorted(pool, key=lambda s: sample_key(s.roaster.roaster_id))[:sample]
    todo = [
        s
        for s in pool
        if (s.status in INGESTABLE or s.roaster.roaster_id in tried) and s.roaster.roaster_id not in done
    ]
    if retry_failed:
        todo = [s for s in todo if s.roaster.roaster_id in tried]
    return todo, mode


def _done_and_tried(lake: Lake, run_date: str) -> tuple[set[str], set[str]]:
    """This run's roasters done (fetched; found closed: unavailable, so asking again won't help until next week; or
    quarantined after crashing two attempts: crashed) and tried at all."""
    con = lake.connect()
    q = f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ?"
    d = [date.fromisoformat(run_date)]
    done = {r[0] for r in con.execute(q + " and status in ('ok', 'unavailable', 'crashed')", d).fetchall()}
    tried = {r[0] for r in con.execute(q, d).fetchall()}
    return done, tried


def _known_bodies(lake: Lake) -> set[str]:
    """Every body_hash the lake already holds: a description is written once, at its first sighting."""
    q = f"select distinct body_hash from {lake.scan('bodies')}"
    return {r[0] for r in lake.connect().execute(q).fetchall()}


def select_parked(
    lake: Lake, run_date: str, adapter: str | None = None
) -> tuple[list[registry.RoasterState], dict[str, dict]]:
    """crawl-retry's work: the run's parked stores still without an ok fetch (of `adapter`'s roasters, when given:
    the parked folder is shared by every connector's partition), and their progress."""
    resume = load_parked(lake, run_date)
    done, _ = retry_transient(_done_and_tried, lake, run_date)  # crawl-retry's first lake read: retried
    states = [
        s
        for s in registry.current(lake)
        if s.roaster.roaster_id in resume.keys() - done and (adapter is None or s.roaster.adapter == adapter)
    ]
    return states, {s.roaster.roaster_id: resume[s.roaster.roaster_id] for s in states}


def manifest_rows(
    roasters: list[Roaster],
    run_date: date,
    manifest_id: str,
    statuses: dict[str, str] | None = None,
    mode: str | None = None,
    code_version: str | None = None,
) -> list[dict]:
    started = datetime.now(UTC).replace(microsecond=0)
    return [
        {
            "run_date": run_date,
            "manifest_id": manifest_id,
            "started_at": started,
            "roaster_id": r.roaster_id,
            "status": (statuses or {}).get(r.roaster_id),
            "platform": r.platform,
            "adapter": r.adapter,
            "mode": mode,
            "code_version": code_version,
        }
        for r in roasters
    ]


def rows_for(
    results: list[Result],
    run_date: date,
    batch_id: str,
    known_bodies: set[str],
    code_version: str | None = None,
) -> dict[str, list[dict]]:
    """The raw rows for a chunk of results (the raw contract: snapshots, fetches, bodies). The crawl-time
    judgements the schema still has columns for (fetches.coffees, snapshots.is_coffee) stay null: the build
    derives them."""
    snaps, fetches, bodies = [], [], []
    for res in results:
        r = res.roaster
        fetches.append(
            {
                "run_date": run_date,
                "batch_id": batch_id,
                "fetched_at": res.fetched_at,
                "roaster_id": r.roaster_id,
                "adapter": r.adapter,
                "status": res.status,
                "error": res.error,
                "products": len(res.items),
                "coffees": None,
                "pages": res.stats.pages,
                "requests": res.stats.requests,
                "http_statuses": [
                    {"status": s, "responses": n} for s, n in sorted(res.stats.statuses.items())
                ],
                "retries": res.stats.retries,
                "rate_limited": res.stats.rate_limited,
                "duration_s": res.duration_s,
                "code_version": code_version,
                "blocks": res.stats.blocks or None,
                "partial": None,  # set by a source that can land a partial catalog (the secondary crawler, #67)
                "crawl_delays": res.stats.crawl_delays or None,
            }
        )
        for it in res.items:
            d = it.model_dump()
            snaps.append(
                d
                | {
                    "run_date": run_date,
                    "batch_id": batch_id,
                    "fetched_at": res.fetched_at,
                    "is_coffee": None,
                }
            )
            if it.body_hash and it.body_hash not in known_bodies:
                known_bodies.add(it.body_hash)
                bodies.append(
                    {"body_hash": it.body_hash, "body_text": it.body_text, "first_seen_run": run_date}
                )
    return {"snapshots": snaps, "fetches": fetches, "bodies": bodies}


def run(
    lake: Lake,
    todo: Sequence[Roaster | registry.RoasterState],
    run_date: str,
    workers: int = 32,
    mode: str | None = None,
    shopify_rps: float = SHOPIFY_RPS,
    per_host: bool = True,
    client_kw: dict | None = None,
    deadline_s: float | None = None,
    resume: dict[str, dict] | None = None,
    platform_rps: dict[str, float] | None = None,
    guard: crashguard.Guard | None = None,
    suspects: set[str] | frozenset[str] = frozenset(),
    quarantine: dict[str, str] | None = None,
) -> dict:
    """Fetch the `todo` roasters for the run. Each one's status (when given as a RoasterState) and `mode` (full, sample,
    only or retry_failed, as select_states returns it) go in the run manifest, which is written, and published to
    a remote lake, before the first request. Only a 'full' manifest is the run's expected set (run_completeness);
    without a mode it is recorded as null, which only adds to that set. resume: parked stores' progress
    (load_parked), so crawl-retry continues each from its blocked page. deadline_s: seconds from now after which no
    new attempt is started (a roaster never tried is left without a fetches row, for a resume to pick up).

    The poison-pill guard (crashguard, from connectors.crawl): guard records each roaster in flight; suspects are
    fetched one at a time, beside the other workers; quarantine (roaster -> error) gets a `crashed` fetches row,
    written before the first request, instead of a fetch. An exception from one roaster's fetch is that roaster's
    error, never the run's."""
    deadline = None if deadline_s is None else time.monotonic() + deadline_s
    skipped = 0
    statuses = {r.roaster.roaster_id: r.status for r in todo if isinstance(r, registry.RoasterState)}
    roasters = [r.roaster if isinstance(r, registry.RoasterState) else r for r in todo]
    rd = date.fromisoformat(run_date)
    version = lineage.code_version()
    manifest = new_batch_id()
    lake.write_partition(
        "run_manifests", run_date, manifest, manifest_rows(roasters, rd, manifest, statuses, mode, version)
    )
    lake.publish(f"ingest {run_date} manifest {manifest}")  # kept even if the job dies before its first chunk
    quarantine = {
        rid: why for rid, why in (quarantine or {}).items() if rid in {r.roaster_id for r in roasters}
    }
    held_out = [Result(r, datetime.now(UTC).replace(microsecond=0), error=quarantine[r.roaster_id][:300],
                       quarantined=True) for r in roasters if r.roaster_id in quarantine]  # fmt: skip
    # suspects first, so the lane starts on them at once
    queue_all = sorted(
        (r for r in roasters if r.roaster_id not in quarantine), key=lambda r: r.roaster_id not in suspects
    )
    known_bodies = retry_transient(_known_bodies, lake)  # the widest scan a crawl makes: retried
    known = known_products(lake)
    stats: Counter = Counter()
    by_adapter: dict[str, Counter] = {}
    failures: list[str] = []
    # A first contact is a store's first attempt in the run date, not in this process: a store an earlier pass of
    # the same run_date already tried (the main pass a resume slot follows, or the crawl a crawl-retry resumes) is
    # never a first contact again, so a resume's refusers don't count twice towards egress-check.
    first_seen: set[str] = set(retry_transient(_done_and_tried, lake, run_date)[1])
    by_ip: dict[str, Counter] = {}

    def flush(chunk: list[Result]) -> None:
        batch = new_batch_id()
        for table, rows in rows_for(chunk, rd, batch, known_bodies, version).items():
            lake.write_partition(table, run_date, batch, rows)
        # Checkpoint: a remote lake gets each chunk as it's done, so a crash or timeout loses at most one
        # chunk, and re-running the week skips the roasters already fetched.
        lake.publish(f"ingest {run_date} batch {batch}")
        for res in chunk:
            a = by_adapter.setdefault(res.roaster.adapter, Counter())
            a["roasters"] += 1
            a["errors"] += bool(res.error) and not res.quarantined  # a quarantined one has its own count
            a["with_products"] += bool(res.items)
            if res.quarantined:  # never asked: no request, no first contact
                a["quarantined"] += 1
                failures.append(f"{res.roaster.roaster_id}: {res.error}")
                continue
            stats["products"] += len(res.items)
            stats["requests"] += res.stats.requests
            stats["http_429"] += res.stats.statuses.get(429, 0)
            stats["http_430"] += res.stats.statuses.get(430, 0)
            if res.roaster.roaster_id not in first_seen:  # its first attempt this run: a first contact
                first_seen.add(res.roaster.roaster_id)
                stats["first_contacts"] += 1
                refused = bool(res.error and res.blocked and not res.stats.pages)
                stats["first_refused"] += refused
                # per egress IP: the IP its first block left from, else the one in use now
                ip = next((b["egress_ip"] for b in res.stats.blocks if b["egress_ip"]), None) or egress_ip()
                by_ip.setdefault(ip or "unknown", Counter()).update(stores=1, refused=int(refused))
            if res.error:
                failures.append(f"{res.roaster.roaster_id}: {res.error}")
            log.info(
                "%-36s %-13s products=%-4d %s",
                res.roaster.roaster_id,
                res.roaster.adapter,
                len(res.items),
                res.error or "",
            )

    # Every roaster is queued at once and results are written as they finish, so a slow site or a publish
    # never leaves the other workers idle.
    # Shopify limits by client IP across all its stores, so its stores share one request budget. Any other platform
    # with a shared budget (a connector's config: connectors.Connector.rps) gets its own, set up the same way.
    rates = {"shopify": shopify_rps} | {k: v for k, v in (platform_rps or {}).items() if k != "shopify"}
    limiters = {
        name: PlatformLimiter(name, rate=rps, max_concurrent=SHOPIFY_CONCURRENCY, per_host=per_host)
        for name, rps in rates.items()
    }
    shopify = limiters["shopify"]
    # Only a store's first block in a run counts towards a platform-wide pause: a retry pass asks only stores that
    # already failed, and a store that blocked in an earlier job of this run (a resume, crawl-retry) is a repeat.
    blocked_before = blocked_hosts(lake, run_date)
    for limiter in limiters.values():
        limiter.count_bursts = mode != "retry_failed"
        limiter.ever_blocked |= blocked_before
    if held_out:
        flush(held_out)
    deferred: dict[str, int] = {}
    not_before: dict[str, float] = {}  # parked roaster -> when its Retry-After (or a backoff) has passed
    retried_ok: dict[str, bool] = {}  # parked roaster -> whether its latest deferred attempt succeeded
    # Roasters that have answered every attempt this run with a block and never served a catalog page.
    refusing: dict[str, int] = {}  # roaster -> attempts so far
    with (
        # the deadline also bounds a host's Crawl-delay: no worker waits one out past it (http.DeadlineReached)
        PoliteClient(limiters=limiters, **({"deadline_s": deadline_s} | (client_kw or {}))) as client,
        ThreadPoolExecutor(workers) as ex,
    ):
        # A failure is written like any other result, in the chunk it finishes in, and every chunk of a round
        # is written before the next round starts: a parked roaster's error row is in the lake before it is
        # retried, so a run that dies during the deferred rounds still shows it attempted (run_is_complete).
        # Its retry is another fetches row; the run counts it ok if any attempt was (int_roaster_runs).
        rounds = DEFERRED_ROUNDS + 1 if per_host else 1
        progress: dict[str, dict] = {k: v | {"backoffs": 0} for k, v in (resume or {}).items()}
        page_stats: Counter = Counter()
        queue = queue_all
        futures: dict = {}  # future -> its roaster
        done: list[Result] = []
        running: set = set()

        def stop_at_deadline(r: Roaster) -> None:
            """r is not started: the deadline passed first. One waiting out a page backoff is parked with its pages
            (crawl-retry resumes it); one never started is left without a fetches row."""
            nonlocal skipped
            p = progress.get(r.roaster_id)
            if p and p.get("backoffs"):
                save_parked(lake, run_date, r.roaster_id, p)
                page_stats["parked"] += 1
                page_stats["parked_at_deadline"] += 1
            else:
                skipped += 1

        def launch(r: Roaster) -> None:
            nonlocal done
            if any(d.roaster.roaster_id == r.roaster_id for d in done):
                # its blocked try's error row goes to the lake before the retry, so a job that dies
                # meanwhile still shows the store attempted (run_is_complete)
                flush(done)
                done = []
            p = progress.get(r.roaster_id)
            resume_at = p and {"page": p["page"], "items": p["items"]}
            last = round_ == rounds - 1
            fut = ex.submit(_attempt, client, r, resume_at, known.get(r.roaster_id), guard, last)
            futures[fut] = r
            running.add(fut)

        for round_ in range(rounds):
            if round_:
                for limiter in limiters.values():
                    limiter.defer = False  # nothing left to overtake: wait out a store's pause in the worker
                deferred[f"round_{round_}"] = len(queue)
            t0, spread = time.monotonic(), DEFERRED_SPREAD if round_ else 0.0
            # (start time, order, roaster): a store is submitted when its time comes, so neither a deferred round's
            # spread nor a page backoff holds a worker while it waits.
            pending = [
                (max(t0, not_before.get(r.roaster_id, 0.0)) + spread * i / len(queue), i, r)
                for i, r in enumerate(queue)
            ]
            heapq.heapify(pending)
            order = len(pending)
            running = set()
            parked: list[Roaster] = []
            done = []
            lane: str | None = None  # the suspect being fetched; the others wait in `held`
            held: deque[Roaster] = deque()
            while pending or running:
                now = time.monotonic()
                if deadline is not None and now >= deadline and pending:
                    # Past the deadline nothing new starts: neither a store due later nor a suspect still waiting
                    # for the lane (one held while in a page backoff keeps its pages too).
                    for _, _, r in pending:
                        stop_at_deadline(r)
                    pending = []
                    for r in held:
                        stop_at_deadline(r)
                    held.clear()
                while pending and pending[0][0] <= now:
                    r = heapq.heappop(pending)[2]
                    if r.roaster_id in suspects:
                        if lane is not None:
                            held.append(r)
                            continue
                        lane = r.roaster_id
                    launch(r)
                timeout = max(0.0, pending[0][0] - now) if pending else None
                # wake at the deadline, not after a long backoff
                if timeout is not None and deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - now))
                if not running:
                    time.sleep(timeout or 0.0)
                    continue
                finished, running = wait_any(running, timeout=timeout, return_when=FIRST_COMPLETED)
                for fut in finished:
                    r = futures.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001 - one roaster's failure, never the run's
                        log.exception("%s: its fetch raised", r.roaster_id)
                        res = Result(
                            r,
                            datetime.now(UTC).replace(microsecond=0),
                            error=f"{type(e).__name__}: {e}"[:300],
                        )
                    rid = res.roaster.roaster_id
                    if rid == lane:
                        lane = None
                        if held and (deadline is None or time.monotonic() < deadline):
                            lane = held[0].roaster_id
                            launch(held.popleft())
                    if round_:
                        retried_ok[rid] = not res.error
                    if res.error and res.blocked and not res.stats.pages and (not round_ or rid in refusing):
                        refusing[rid] = refusing.get(rid, 0) + 1
                    else:
                        refusing.pop(rid, None)
                    if res.progress is not None:  # blocked on a page: back off, then that page again
                        p = progress.setdefault(rid, {"backoffs": 0})
                        p |= {k: res.progress[k] for k in ("page", "items", "host")}
                        n = min(p["backoffs"], len(PAGE_BACKOFFS) - 1)
                        wait = min(MAX_STORE_WAIT, max(res.progress["wait"] or 0.0, PAGE_BACKOFFS[n]))
                        due = time.monotonic() + wait
                        past_deadline = deadline is not None and due >= deadline
                        if p["backoffs"] < len(PAGE_BACKOFFS) and not past_deadline:
                            p["backoffs"] += 1
                            page_stats["backoffs"] += 1
                            res.error = (
                                f"backoff {p['backoffs']} of {len(PAGE_BACKOFFS)} ({wait:.0f}s, then page "
                                f"{p['page']} again): {res.error}"
                            )[:300]
                            for limiter in limiters.values():  # its page backoff is its own pause
                                limiter.quiet_hosts.add(res.progress["host"])
                            heapq.heappush(pending, (due, order, res.roaster))
                            order += 1
                        else:  # backoffs spent, or the next one would end past the deadline: parked now
                            save_parked(lake, run_date, rid, p)
                            page_stats["parked"] += 1
                            never = p["page"] == 1 and not p["items"]
                            why = f"still blocked after {len(PAGE_BACKOFFS)} backoffs"
                            if p["backoffs"] < len(PAGE_BACKOFFS):
                                page_stats["parked_at_deadline"] += 1
                                why = f"blocked; its {wait:.0f}s backoff would end past the deadline"
                            res.error = (
                                f"{'refused' if never else 'parked'}: page {p['page']} {why}; crawl-retry resumes "
                                f"it; last {res.error}"
                            )[:300]
                            if never:
                                page_stats["parked_never_served"] += 1
                    else:
                        if not res.error:
                            quiet = progress.pop(rid, {}).get("host", "")
                            for limiter in limiters.values():
                                limiter.quiet_hosts.discard(quiet)
                            parked_path(lake, run_date, rid).unlink(missing_ok=True)  # crawl-retry got it
                        last = not (res.error and res.deferrable) or round_ == rounds - 1
                        # A pause (a block, or a 5xx's long Retry-After): not asked again before its Retry-After
                        # (at most MAX_STORE_WAIT), else a backoff. Never scheduled past the deadline: that store keeps
                        # this error row, and the next pass (a resume, crawl-retry, the retry pass) asks again.
                        paused = res.blocked or res.wait is not None
                        backoff = DEFERRED_BACKOFFS[min(round_, len(DEFERRED_BACKOFFS) - 1)]
                        due = time.monotonic() + (
                            store_wait(max(res.wait or 0.0, backoff)) if paused else 0.0
                        )
                        if not last and paused and deadline is not None and due >= deadline:
                            last = True
                            deferred["past_deadline"] = deferred.get("past_deadline", 0) + 1
                            res.error = f"not retried: its wait ends past the run's deadline; {res.error}"[
                                :300
                            ]
                        if last and rid in refusing and round_:
                            res.error = (
                                f"refused: answered a block to every catalog request in this run "
                                f"({refusing[rid]} attempts); last {res.error}"
                            )[:300]
                        if not last:
                            parked.append(res.roaster)
                            if paused:
                                not_before[rid] = due
                    done.append(res)
                    if len(done) == CHUNK:
                        flush(done)
                        done = []
            for r in held:  # suspects the lane never reached before the deadline
                stop_at_deadline(r)
            held.clear()
            if done:
                flush(done)
            queue = parked
            if not queue or (deadline is not None and time.monotonic() >= deadline):
                break
        if retried_ok:
            deferred["still_failing"] = sum(not ok for ok in retried_ok.values())
            deferred["refused"] = sum(1 for rid, ok in retried_ok.items() if not ok and rid in refusing)
        deferred |= {f"page_{k}": v for k, v in page_stats.items()}
    return {
        "run_date": run_date,
        "roasters": len(roasters),
        "products": stats["products"],
        "by_adapter": {k: dict(v) for k, v in sorted(by_adapter.items())},
        "failures": failures[:50],
        "quarantined": sorted(quarantine),  # not fetched: a `crashed` row each (crashguard)
        "shopify_limiter": dict(shopify.stats),  # requests, 429s, platform-wide pauses, stores paused/parked
        "limiters": {
            name: dict(limiter.stats) for name, limiter in limiters.items()
        },  # the same, per platform
        "deferred": deferred,  # roasters per deferred round, and those still failing after them
        "skipped_deadline": skipped,  # not started: the deadline passed first
        "requests": stats["requests"],
        "http_429": stats["http_429"],
        "http_430": stats["http_430"],
        # stores whose first catalog attempt this run was refused (429/430/limiter) before serving a page
        "first_contact": {
            "stores": stats["first_contacts"],
            "refused": stats["first_refused"],
            "by_ip": {ip: dict(c) for ip, c in sorted(by_ip.items())},
        },  # fmt: skip
    }
