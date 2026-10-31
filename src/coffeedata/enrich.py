"""Claude enrichment for coffees the rules couldn't fully describe: the post-load step (`coffeedata enrich-pending`).

ELT: ingestion only loads snapshots, bodies and fetches. This step reads them back from the lake and derives the
candidates the way the build does (products.derive, stg_products' logic, with stored verdicts): a (roaster,
product, body_hash) needs Claude when its latest version is coffee, the rules leave attributes missing
(extract.needs_llm), it has a description, no llm row exists yet, and it isn't in a submitted-but-uncollected
batch. Work is derived, not tracked: anything skipped (over the cap, API error, failed batch item) is simply
picked up again next run.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import cast

from . import extract, products
from .lake import Lake, new_batch_id
from .models import Enrichment

log = logging.getLogger(__name__)

SYNC_LIMIT = 100

VERSIONS_SQL = """
with latest as (
    select s.roaster_id, s.product_id, s.body_hash, s.title, s.product_type, s.tags,
           list_transform(s.variants, lambda v: v.grams) as grams, {input_hash} as input_hash,
           min(s.fetched_at) over (partition by s.roaster_id, s.product_id, s.body_hash) as first_seen
    from {snapshots} s
    where s.roaster_id is not null
    qualify row_number() over (partition by s.roaster_id, s.product_id, s.body_hash order by s.fetched_at desc) = 1
),
llm as (select distinct roaster_id, product_id, body_hash from {enrichments} where method = 'llm'),
b as (select * from {llm_batches}),
in_flight as (
    select distinct roaster_id, product_id, body_hash from b where state = 'submitted'
      and batch_id not in (select batch_id from b where state = 'collected')
)
select l.*, bo.body_text
from latest l
join (select body_hash, any_value(body_text) as body_text from {bodies} group by 1) bo using (body_hash)
{anti}
where coalesce(bo.body_text, '') <> ''
order by l.first_seen, l.roaster_id, l.product_id
"""


def _versions(lake: Lake, pending_only: bool = True) -> list[dict]:
    from .verdict import INPUT_HASH

    anti = (
        "anti join llm using (roaster_id, product_id, body_hash)\n"
        "anti join in_flight using (roaster_id, product_id, body_hash)"
        if pending_only
        else ""
    )
    sql = VERSIONS_SQL.format(
        input_hash=INPUT_HASH,
        anti=anti,
        **{t: lake.scan(t) for t in ("enrichments", "llm_batches", "snapshots", "bodies")},
    )
    cur = lake.connect().execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def pending(lake: Lake, limit: int) -> list[dict]:
    """The Claude candidates, oldest first: each with its title, tags, body_text and the rules' Enrichment (base)."""
    from . import verdict

    known = products.verdicts(lake, verdict.model(), verdict.DEFINITION_VERSION)
    out = []
    for r in _versions(lake):
        derived, base = products.derive(r, known)
        if not derived["is_coffee"] or base is None or not extract.needs_llm(base):
            continue
        out.append(
            {k: r[k] for k in ("roaster_id", "product_id", "body_hash", "title", "tags", "body_text")}
            | {"base": base}
        )
        if len(out) >= limit:
            break
    return out


def _row(p: dict, e: Enrichment, at: datetime) -> dict:
    return e.model_dump() | {
        "extracted_at": at,
        "roaster_id": p["roaster_id"],
        "product_id": p["product_id"],
        "body_hash": p["body_hash"],
        "needs_llm": False,
    }


def run(lake: Lake, limit: int = 2000, run_date: str | None = None) -> dict:
    """Enrich up to `limit` pending coffees: synchronously when few, via the Batches API when many."""
    import anthropic

    todo = pending(lake, limit)
    if not todo:
        return {"pending": 0}
    run_date = run_date or date.today().isoformat()
    client = extract.client()
    now = datetime.now(UTC).replace(microsecond=0)
    if len(todo) <= SYNC_LIMIT:
        rows, errors = [], 0
        for p in todo:
            try:
                msg = client.messages.create(
                    **extract.llm_request(p["title"], p["tags"] or [], p["body_text"])
                )
            except anthropic.APIError as e:  # status, connection and timeout errors alike
                errors += 1
                log.warning("enrich %s:%s failed: %s", p["roaster_id"], p["product_id"], e)
                continue
            if e2 := extract.parse_llm_message(msg, p["base"]):
                rows.append(_row(p, e2, now))
        lake.write_partition("enrichments", run_date, new_batch_id(), rows)
        return {"pending": len(todo), "enriched": len(rows), "errors": errors}
    return submit_batch(lake, client, todo, now)


def submit_batch(lake: Lake, client, todo: list[dict], now: datetime) -> dict:
    from anthropic import APIError
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    reqs = [
        Request(
            custom_id=f"e{i}",
            params=cast(
                MessageCreateParamsNonStreaming,
                extract.llm_request(p["title"], p["tags"] or [], p["body_text"]),
            ),
        )
        for i, p in enumerate(todo)
    ]
    try:
        batch = client.messages.batches.create(requests=reqs)
    except APIError as e:
        log.warning("batch submit failed: %s", e)
        return {"pending": len(todo), "submitted": 0, "error": str(e)}
    lake.write(
        "llm_batches",
        f"llm_batches/state=submitted/{batch.id}.parquet",
        [
            {
                "batch_id": batch.id,
                "state": "submitted",
                "at": now,
                "custom_id": f"e{i}",
                "roaster_id": p["roaster_id"],
                "product_id": p["product_id"],
                "body_hash": p["body_hash"],
            }
            for i, p in enumerate(todo)
        ],
    )
    return {"pending": len(todo), "submitted": len(reqs), "batch_id": batch.id}


def collect(lake: Lake, run_date: str | None = None) -> dict:
    """Write results of finished batches as llm enrichment rows, then mark each batch collected."""
    import anthropic

    con = lake.connect()
    open_batches = [
        r[0]
        for r in con.execute(
            f"""select distinct batch_id from {lake.scan("llm_batches")} where state = 'submitted'
            and batch_id not in (select batch_id from {lake.scan("llm_batches")} where state = 'collected')"""
        ).fetchall()
    ]
    if not open_batches:
        return {"collected": 0}
    client = extract.client()
    run_date = run_date or date.today().isoformat()
    merged = 0
    for batch_id in open_batches:
        try:
            if client.messages.batches.retrieve(batch_id).processing_status != "ended":
                continue
            results = list(client.messages.batches.results(batch_id))
        except anthropic.APIError as e:
            log.warning("batch %s: %s", batch_id, e)
            continue
        meta = _batch_meta(con, lake, batch_id)
        bases = _bases(con, lake, list(meta.values()))
        now = datetime.now(UTC).replace(microsecond=0)
        rows = []
        for res in results:
            m = meta.get(res.custom_id)
            if not m or res.result.type != "succeeded":
                continue  # failed items have no llm row, so they are pending again next run
            key = (m["roaster_id"], m["product_id"], m["body_hash"])
            if found := extract.parse_llm_message(res.result.message, bases.get(key, Enrichment())):
                rows.append(_row(m, found, now))
        lake.write_partition("enrichments", run_date, new_batch_id(), rows)
        lake.write(
            "llm_batches",
            f"llm_batches/state=collected/{batch_id}.parquet",
            [{"batch_id": batch_id, "state": "collected", "at": now}],
        )
        merged += len(rows)
    return {"collected": merged}


def _batch_meta(con, lake: Lake, batch_id: str) -> dict[str, dict]:
    cur = con.execute(
        f"select custom_id, roaster_id, product_id, body_hash from {lake.scan('llm_batches')} "
        "where batch_id = ? and state = 'submitted'",
        [batch_id],
    )
    return {r[0]: {"roaster_id": r[1], "product_id": r[2], "body_hash": r[3]} for r in cur.fetchall()}


def _bases(con, lake: Lake, keys: list[dict]) -> dict[tuple, Enrichment]:
    """The rules' attributes for each key's latest version, derived again (no rules rows are stored)."""
    wanted = {(k["roaster_id"], k["product_id"], k["body_hash"]) for k in keys}
    out = {}
    for r in _versions(lake, pending_only=False):
        key = (r["roaster_id"], r["product_id"], r["body_hash"])
        if key in wanted:
            out[key] = extract.rules(products.item(r))
    return out
