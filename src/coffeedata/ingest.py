"""Weekly pipeline: fetch every roaster with a usable storefront, append the raw results to the lake.

Writes (never rewrites): snapshots (every product, coffee or not), fetches (one row per roaster attempt,
ok or error), bodies (description text, once per new body_hash) and rule-based enrichments (once per new
body_hash). Re-running for the same run date only fetches roasters without a successful fetch that day.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from . import extract, registry
from .adapters import fetch_catalog
from .classify import is_coffee
from .http import PoliteClient
from .lake import Lake, new_batch_id
from .models import CatalogItem, Roaster

log = logging.getLogger(__name__)

INGESTABLE = {"pending", "active", "no_coffee", "no_catalog"}
CHUNK = 200  # roasters per written and published batch: a crash loses at most one chunk


@dataclass
class Result:
    roaster: Roaster
    fetched_at: datetime
    items: list[CatalogItem] = field(default_factory=list)
    error: str | None = None

    @property
    def coffees(self) -> list[CatalogItem]:
        return [i for i in self.items if is_coffee(i)]


def ingest_one(client: PoliteClient, roaster: Roaster) -> Result:
    res = Result(roaster, datetime.now(UTC).replace(microsecond=0))
    try:
        res.items = list({i.product_id: i for i in fetch_catalog(client, roaster)}.values())  # feeds repeat items
    except Exception as e:  # noqa: BLE001 - any failure is this roaster's error, never the run's
        res.error = f"{type(e).__name__}: {e}"[:300]
        res.items = []
    return res


def sample_key(roaster_id: str) -> str:
    """Stable order for --sample: unaffected by roasters being added, shared by probe and ingest."""
    return hashlib.sha1(roaster_id.encode()).hexdigest()


def select(lake: Lake, run_date: str, only: str | None = None, sample: int | None = None,
           retry_failed: bool = False) -> list[Roaster]:
    states = registry.current(lake)
    if only:
        return [s.roaster for s in states if s.roaster.roaster_id == only]
    con = lake.connect()
    done = {r[0] for r in con.execute(
        f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ? and status = 'ok'",
        [date.fromisoformat(run_date)]).fetchall()}
    tried = {r[0] for r in con.execute(
        f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ?", [date.fromisoformat(run_date)]
    ).fetchall()}
    # Draw a sample from a population that doesn't change as the run progresses, so re-running the same
    # command picks the same roasters (and then skips the ones already done).
    pool = sorted((s for s in states if s.roaster.platform and s.status != "opted_out"),
                  key=lambda s: s.roaster.roaster_id)
    if sample:  # a hash order is stable as roasters are added, so weekly samples keep the same roasters
        pool = sorted(pool, key=lambda s: sample_key(s.roaster.roaster_id))[:sample]
    todo = [s.roaster for s in pool if (s.status in INGESTABLE or s.roaster.roaster_id in tried)
            and s.roaster.roaster_id not in done]
    if retry_failed:
        todo = [r for r in todo if r.roaster_id in tried]
    return todo


def rows_for(results: list[Result], run_date: date, batch_id: str, known_bodies: set[str],
             enriched: set[tuple[str, str, str]]) -> dict[str, list[dict]]:
    snaps, fetches, bodies, enrich = [], [], [], []
    for res in results:
        r = res.roaster
        coffee_ids = {i.product_id for i in res.coffees}
        fetches.append({"run_date": run_date, "batch_id": batch_id, "fetched_at": res.fetched_at,
                        "roaster_id": r.roaster_id, "adapter": r.adapter, "status": "error" if res.error else "ok",
                        "error": res.error, "products": len(res.items), "coffees": len(coffee_ids)})
        for it in res.items:
            d = it.model_dump()
            snaps.append(d | {"run_date": run_date, "batch_id": batch_id, "fetched_at": res.fetched_at,
                              "is_coffee": it.product_id in coffee_ids})
            if it.body_hash and it.body_hash not in known_bodies:
                known_bodies.add(it.body_hash)
                bodies.append({"body_hash": it.body_hash, "body_text": it.body_text, "first_seen_run": run_date})
            key = (r.roaster_id, it.product_id, it.body_hash)
            if it.product_id in coffee_ids and key not in enriched:
                enriched.add(key)
                e = extract.rules(it)
                enrich.append(e.model_dump() | {"extracted_at": res.fetched_at, "roaster_id": r.roaster_id,
                                                "product_id": it.product_id, "needs_llm": extract.needs_llm(e)})
    return {"snapshots": snaps, "fetches": fetches, "bodies": bodies, "enrichments": enrich}


def run(lake: Lake, roasters: list[Roaster], run_date: str, workers: int = 32) -> dict:
    rd = date.fromisoformat(run_date)
    con = lake.connect()
    known_bodies = {r[0] for r in con.execute(f"select distinct body_hash from {lake.scan('bodies')}").fetchall()}
    enriched = {tuple(r) for r in con.execute(
        f"select distinct roaster_id, product_id, body_hash from {lake.scan('enrichments')}").fetchall()}
    stats: Counter = Counter()
    by_adapter: dict[str, Counter] = {}
    failures: list[str] = []
    with PoliteClient() as client, ThreadPoolExecutor(workers) as ex:
        for start in range(0, len(roasters), CHUNK):
            chunk = list(ex.map(lambda r: ingest_one(client, r), roasters[start:start + CHUNK]))
            batch = new_batch_id()
            for table, rows in rows_for(chunk, rd, batch, known_bodies, enriched).items():
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
                log.info("%-36s %-13s products=%-4d coffees=%-4d %s", res.roaster.roaster_id, res.roaster.adapter,
                         len(res.items), len(res.coffees), res.error or "")
    return {"run_date": run_date, "roasters": len(roasters), "products": stats["products"],
            "by_adapter": {k: dict(v) for k, v in sorted(by_adapter.items())}, "failures": failures[:50]}
