"""Daily pipeline: fetch every active roaster's catalog, keep the coffees, store, enrich, track coverage."""

from __future__ import annotations

import json
import logging
import random
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from . import extract, registry, store
from .adapters import fetch_catalog
from .classify import is_coffee
from .http import Blocked, PoliteClient
from .models import CatalogItem, Enrichment, Roaster

log = logging.getLogger(__name__)

INGESTABLE = {"pending", "active", "no_coffee"}
MAX_FAILURES = 3
STATE = Path("data/state")


@dataclass
class Result:
    slug: str
    adapter: str
    products: int = 0
    coffees: int = 0
    events: int = 0
    error: str | None = None
    to_llm: list[CatalogItem] = field(default_factory=list)
    base: dict[str, Enrichment] = field(default_factory=dict)


def select(rows: list[dict], only: str | None = None, sample: int | None = None,
           shard: tuple[int, int] | None = None) -> list[dict]:
    todo = [r for r in rows if r.get("status", "pending") in INGESTABLE and r.get("platform") not in ("", None)]
    if only:
        todo = [r for r in rows if only in (r["slug"], r["domain"])]
    if shard:
        i, n = shard
        todo = [r for r in todo if zlib.crc32(r["slug"].encode()) % n == i]
    if sample:
        rng = random.Random(date.today().toordinal())
        todo = rng.sample(todo, min(sample, len(todo)))
    return todo


def ingest_one(client: PoliteClient, roaster: Roaster, data_root: Path, enriched_root: Path) -> Result:
    res = Result(roaster.slug, roaster.adapter)
    try:
        items = fetch_catalog(client, roaster)
    except (httpx.HTTPError, Blocked, ValueError, KeyError) as e:
        res.error = f"{type(e).__name__}: {e}"[:200]
        return res
    res.products = len(items)
    coffees = [i for i in items if is_coffee(i)]
    res.coffees = len(coffees)
    res.events = len(store.save(roaster.slug, coffees, data_root))
    cache = extract.load_cache(roaster.slug, enriched_root)
    for it in coffees:
        cached = cache.get(it.product_id)
        if cached and cached.get("body_hash") == it.body_hash:
            continue
        e = extract.rules(it)
        cache[it.product_id] = e.model_dump()
        if extract.needs_llm(e) and it.body_text:
            res.to_llm.append(it)
            res.base[it.product_id] = e
    live = {i.product_id for i in coffees}
    cache = {k: v for k, v in cache.items() if k in live}
    if cache or (enriched_root / f"{roaster.slug}.json").exists():
        extract.save_cache(roaster.slug, cache, enriched_root)
    return res


def update_row(row: dict, res: Result) -> None:
    today = date.today().isoformat()
    if res.error:
        row["failures"] = str(int(row.get("failures") or 0) + 1)
        if int(row["failures"]) >= MAX_FAILURES:
            row["status"] = "unreachable"
        return
    row.update(failures="0", last_ingested=today, coffees=str(res.coffees))
    row["status"] = "active" if res.coffees else ("no_coffee" if res.products else "no_catalog")


def run(rows: list[dict], todo: list[dict], workers: int = 16, data_root: Path = store.DATA,
        enriched_root: Path = extract.ENRICHED, enrich_max: int = 2000) -> dict:
    results: list[Result] = []
    roasters = registry.roasters(todo)
    with PoliteClient() as client, ThreadPoolExecutor(workers) as ex:
        for res in ex.map(lambda r: ingest_one(client, r, data_root, enriched_root), roasters):
            log.info("%-32s %-11s products=%-4d coffees=%-4d events=%-4d %s", res.slug, res.adapter,
                     res.products, res.coffees, res.events, res.error or "")
            results.append(res)
    by_slug = {r["slug"]: r for r in rows}
    for res in results:
        update_row(by_slug[res.slug], res)
    llm = 0
    if extract.llm_enabled():
        llm = enrich_with_llm(results, enriched_root, enrich_max)
    return coverage(rows, results, llm, data_root)


def enrich_with_llm(results: list[Result], enriched_root: Path, enrich_max: int) -> int:
    """Small runs go synchronously; big backfills go through the Message Batches API (collected next run)."""
    pending = [(r, it) for r in results for it in r.to_llm][:enrich_max]
    if not pending:
        return 0
    if len(pending) <= 100:
        done = 0
        for r in results:
            items = [it for rr, it in pending if rr is r]
            if not items:
                continue
            out = extract.enrich_llm_sync(items, r.base)
            cache = extract.load_cache(r.slug, enriched_root)
            for pid, e in out.items():
                cache[pid] = e.model_dump()
            extract.save_cache(r.slug, cache, enriched_root)
            done += len(out)
        return done
    return submit_batch(pending)


def submit_batch(pending: list[tuple[Result, CatalogItem]]) -> int:
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = anthropic.Anthropic()
    reqs, base = [], {}
    for i, (r, it) in enumerate(pending):
        cid = f"e{i}"
        reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**extract.llm_request(it))))
        base[cid] = {"slug": r.slug, "product_id": it.product_id, "base": r.base[it.product_id].model_dump()}
    batch = client.messages.batches.create(requests=reqs)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"batch-{batch.id}.json").write_text(json.dumps(base, sort_keys=True))
    log.info("submitted enrichment batch %s (%d items)", batch.id, len(reqs))
    return 0


def collect_batches(enriched_root: Path = extract.ENRICHED) -> int:
    """Merge results of any finished enrichment batches into the cache."""
    import anthropic

    if not STATE.exists():
        return 0
    client = anthropic.Anthropic()
    merged = 0
    for f in sorted(STATE.glob("batch-*.json")):
        batch_id = f.stem.removeprefix("batch-")
        if client.messages.batches.retrieve(batch_id).processing_status != "ended":
            continue
        meta = json.loads(f.read_text())
        caches: dict[str, dict] = {}
        for res in client.messages.batches.results(batch_id):
            m = meta.get(res.custom_id)
            if not m or res.result.type != "succeeded":
                continue
            e = extract.parse_llm_message(res.result.message, Enrichment(**m["base"]))
            if e:
                cache = caches.setdefault(m["slug"], extract.load_cache(m["slug"], enriched_root))
                if m["product_id"] in cache:  # product may have been removed since submission
                    cache[m["product_id"]] = e.model_dump()
                    merged += 1
        for slug, cache in caches.items():
            extract.save_cache(slug, cache, enriched_root)
        f.unlink()
    return merged


def coverage(rows: list[dict], results: list[Result], llm: int, data_root: Path) -> dict:
    by_adapter: dict[str, Counter] = {}
    for r in results:
        c = by_adapter.setdefault(r.adapter, Counter())
        c["roasters"] += 1
        c["with_coffee"] += bool(r.coffees)
        c["coffees"] += r.coffees
        c["errors"] += bool(r.error)
    cov = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "registry": dict(sorted(Counter(r.get("status", "pending") for r in rows).items())),
        "platforms": dict(sorted(Counter(r.get("platform") or "unknown" for r in rows).items())),
        "this_run": {k: dict(v) for k, v in sorted(by_adapter.items())},
        "coffees_this_run": sum(r.coffees for r in results),
        "events_this_run": sum(r.events for r in results),
        "llm_enriched_this_run": llm,
        "failures": sorted(f"{r.slug}: {r.error}" for r in results if r.error)[:200],
    }
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "coverage.json").write_text(json.dumps(cov, indent=1, sort_keys=True) + "\n")
    return cov
