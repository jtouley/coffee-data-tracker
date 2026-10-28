"""Weekly pipeline: fetch every roaster with a usable storefront, append the raw results to the lake.

Writes (never rewrites): a run manifest (the roasters this ingest sets out to fetch, before the first request),
snapshots (every product, coffee or not), fetches (one row per roaster attempt, ok or error, with its request
diagnostics), bodies (description text, once per new body_hash) and rule-based enrichments (once per new
body_hash). Re-running for the same run date only fetches roasters without a successful fetch that day.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from . import extract, lineage, registry
from .adapters import fetch_catalog
from .classify import is_coffee
from .http import PoliteClient, RequestStats
from .lake import Lake, new_batch_id
from .models import CatalogItem, Roaster
from .ratelimit import SHOPIFY_CONCURRENCY, SHOPIFY_RPS, PlatformLimiter

log = logging.getLogger(__name__)

INGESTABLE = {"pending", "active", "no_coffee", "no_catalog"}
CHUNK = 200  # roasters per written and published batch: a crash loses at most one chunk


@dataclass
class Result:
    roaster: Roaster
    fetched_at: datetime
    items: list[CatalogItem] = field(default_factory=list)
    error: str | None = None
    stats: RequestStats = field(default_factory=RequestStats)
    duration_s: float | None = None

    @property
    def coffees(self) -> list[CatalogItem]:
        return [i for i in self.items if is_coffee(i)]


def ingest_one(client: PoliteClient, roaster: Roaster) -> Result:
    res = Result(roaster, datetime.now(UTC).replace(microsecond=0))
    start = time.monotonic()
    with client.track() as res.stats:
        try:
            res.items = list(
                {i.product_id: i for i in fetch_catalog(client, roaster)}.values()
            )  # feeds repeat items
        except Exception as e:  # noqa: BLE001 - any failure is this roaster's error, never the run's
            res.error = f"{type(e).__name__}: {e}"[:300]
            res.items = []
    res.duration_s = round(time.monotonic() - start, 3)
    return res


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
    con = lake.connect()
    done = {
        r[0]
        for r in con.execute(
            f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ? and status = 'ok'",
            [date.fromisoformat(run_date)],
        ).fetchall()
    }
    tried = {
        r[0]
        for r in con.execute(
            f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ?",
            [date.fromisoformat(run_date)],
        ).fetchall()
    }
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
    enriched: set[tuple[str, str, str]],
    code_version: str | None = None,
) -> dict[str, list[dict]]:
    snaps, fetches, bodies, enrich = [], [], [], []
    for res in results:
        r = res.roaster
        coffee_ids = {i.product_id for i in res.coffees}
        fetches.append(
            {
                "run_date": run_date,
                "batch_id": batch_id,
                "fetched_at": res.fetched_at,
                "roaster_id": r.roaster_id,
                "adapter": r.adapter,
                "status": "error" if res.error else "ok",
                "error": res.error,
                "products": len(res.items),
                "coffees": len(coffee_ids),
                "pages": res.stats.pages,
                "requests": res.stats.requests,
                "http_statuses": [
                    {"status": s, "responses": n} for s, n in sorted(res.stats.statuses.items())
                ],
                "retries": res.stats.retries,
                "rate_limited": res.stats.rate_limited,
                "duration_s": res.duration_s,
                "code_version": code_version,
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
                    "is_coffee": it.product_id in coffee_ids,
                }
            )
            if it.body_hash and it.body_hash not in known_bodies:
                known_bodies.add(it.body_hash)
                bodies.append(
                    {"body_hash": it.body_hash, "body_text": it.body_text, "first_seen_run": run_date}
                )
            key = (r.roaster_id, it.product_id, it.body_hash)
            if it.product_id in coffee_ids and key not in enriched:
                enriched.add(key)
                e = extract.rules(it)
                enrich.append(
                    e.model_dump()
                    | {
                        "extracted_at": res.fetched_at,
                        "roaster_id": r.roaster_id,
                        "product_id": it.product_id,
                        "needs_llm": extract.needs_llm(e),
                    }
                )
    return {"snapshots": snaps, "fetches": fetches, "bodies": bodies, "enrichments": enrich}


def run(
    lake: Lake,
    todo: Sequence[Roaster | registry.RoasterState],
    run_date: str,
    workers: int = 32,
    mode: str | None = None,
    shopify_rps: float = SHOPIFY_RPS,
) -> dict:
    """Fetch the `todo` roasters for the run. Each one's status (when given as a RoasterState) and `mode` (full, sample,
    only or retry_failed, as select_states returns it) go in the run manifest, which is written, and published to
    a remote lake, before the first request. Only a 'full' manifest is the run's expected set (run_completeness);
    without a mode it is recorded as null, which only adds to that set."""
    statuses = {r.roaster.roaster_id: r.status for r in todo if isinstance(r, registry.RoasterState)}
    roasters = [r.roaster if isinstance(r, registry.RoasterState) else r for r in todo]
    rd = date.fromisoformat(run_date)
    version = lineage.code_version()
    manifest = new_batch_id()
    lake.write_partition(
        "run_manifests", run_date, manifest, manifest_rows(roasters, rd, manifest, statuses, mode, version)
    )
    lake.publish(f"ingest {run_date} manifest {manifest}")  # kept even if the job dies before its first chunk
    con = lake.connect()
    known_bodies = {
        r[0] for r in con.execute(f"select distinct body_hash from {lake.scan('bodies')}").fetchall()
    }
    enriched = {
        tuple(r)
        for r in con.execute(
            f"select distinct roaster_id, product_id, body_hash from {lake.scan('enrichments')}"
        ).fetchall()
    }
    stats: Counter = Counter()
    by_adapter: dict[str, Counter] = {}
    failures: list[str] = []

    def flush(chunk: list[Result]) -> None:
        batch = new_batch_id()
        for table, rows in rows_for(chunk, rd, batch, known_bodies, enriched, version).items():
            lake.write_partition(table, run_date, batch, rows)
        # Checkpoint: a remote lake gets each chunk as it's done, so a crash or timeout loses at most one
        # chunk, and re-running the week skips the roasters already fetched.
        lake.publish(f"ingest {run_date} batch {batch}")
        for res in chunk:
            a = by_adapter.setdefault(res.roaster.adapter, Counter())
            a["roasters"] += 1
            a["errors"] += bool(res.error)
            a["with_coffee"] += bool(res.coffees)
            a["coffees"] += len(res.coffees)
            stats["products"] += len(res.items)
            if res.error:
                failures.append(f"{res.roaster.roaster_id}: {res.error}")
            log.info(
                "%-36s %-13s products=%-4d coffees=%-4d %s",
                res.roaster.roaster_id,
                res.roaster.adapter,
                len(res.items),
                len(res.coffees),
                res.error or "",
            )

    # Every roaster is queued at once and results are written as they finish, so a slow site or a publish
    # never leaves the other workers idle.
    # Shopify limits by client IP across all its stores, so its stores share one request budget.
    shopify = PlatformLimiter("shopify", rate=shopify_rps, max_concurrent=SHOPIFY_CONCURRENCY)
    with PoliteClient(limiters={"shopify": shopify}) as client, ThreadPoolExecutor(workers) as ex:
        done: list[Result] = []
        for fut in as_completed([ex.submit(ingest_one, client, r) for r in roasters]):
            done.append(fut.result())
            if len(done) == CHUNK:
                flush(done)
                done = []
        if done:
            flush(done)
    return {
        "run_date": run_date,
        "roasters": len(roasters),
        "products": stats["products"],
        "by_adapter": {k: dict(v) for k, v in sorted(by_adapter.items())},
        "failures": failures[:50],
        "shopify_limiter": dict(shopify.stats),  # requests, 429s, platform-wide pauses
    }
