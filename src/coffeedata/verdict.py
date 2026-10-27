"""Claude's verdict on whether each product version is a coffee, stored in the lake.

The rules (classify.py) measure about 94% on products labeled by hand; the long tail of things roasters sell next to
their beans (seeds, salt, rubs, canisters, oils, drinks) is more than rules can list. So every product version the
rules don't exclude outright is shown to Claude with the same written definition the labels were made against
(coffee_definition.md), and the answer is stored like any fetched fact. stg_products uses it when present, the rules
otherwise, so a build stays a pure function of the lake.

Opt-in: nothing runs unless COFFEEDATA_VERDICT_MODEL names a model. Work is derived, not tracked: a product version
is pending when it has no verdict for this model and definition and isn't in an open batch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, date, datetime
from importlib import resources
from typing import cast

from .classify import excluded
from .lake import Lake, new_batch_id
from .models import CatalogItem, Variant

log = logging.getLogger(__name__)

DEFINITION = resources.files("coffeedata").joinpath("coffee_definition.md").read_text()
# Verdicts are kept per definition: changing the definition makes every product pending again.
DEFINITION_VERSION = hashlib.sha256(DEFINITION.encode()).hexdigest()[:12]
LABELS = ("coffee", "not_coffee", "unsure")
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label"],
    "properties": {"label": {"type": "string", "enum": list(LABELS)}},
}
SYSTEM = (
    "You decide whether a product from a US coffee roaster's online store belongs in a catalog of coffees. "
    "Apply this definition exactly and answer with one label.\n\n" + DEFINITION
)

# The same key stg_snapshots computes, so verdicts join to snapshots in dbt.
INPUT_HASH = (
    "md5(to_json(struct_pack(s.title, s.product_type, s.tags, s.body_hash, "
    "grams := list_transform(s.variants, lambda v: v.grams))))"
)
PENDING_SQL = """
with v as (
    select distinct on (input_hash) {input_hash} as input_hash, s.title, s.product_type, s.tags, s.body_hash,
           list_transform(s.variants, lambda v: v.grams) as grams
    from {snapshots} s
),
done as (select distinct input_hash from {verdicts} where model = $model and definition = $definition),
b as (select * from {verdict_batches} where model = $model and definition = $definition),
in_flight as (
    select distinct custom_id as input_hash from b where state = 'submitted'
      and batch_id not in (select batch_id from b where state = 'collected')
)
select v.*, bo.body_text
from v
left join (select body_hash, any_value(body_text) as body_text from {bodies} group by 1) bo using (body_hash)
anti join done using (input_hash)
anti join in_flight using (input_hash)
order by input_hash
"""


def model() -> str | None:
    return os.environ.get("COFFEEDATA_VERDICT_MODEL") or None


def item(r: dict) -> CatalogItem:
    return CatalogItem(
        roaster_id="",
        product_id="",
        handle="",
        url="",
        title=r["title"] or "",
        platform="",
        product_type=r["product_type"] or "",
        tags=r["tags"] or [],
        variants=[Variant(variant_id=str(i), grams=g) for i, g in enumerate(r["grams"] or [])],
        body_text=r.get("body_text") or "",
    )


def listing(it: CatalogItem) -> str:
    sizes = sorted({v.grams for v in it.variants if v.grams})
    return (
        f"Title: {it.title}\nProduct type: {it.product_type}\nTags: {', '.join(it.tags)}\n"
        f"Bag sizes (g): {', '.join(map(str, sizes)) or 'not stated'}\n\nDescription:\n{it.body_text[:1500]}"
    )


def request(it: CatalogItem, model_id: str) -> dict:
    return {
        "model": model_id,
        "max_tokens": 1024,
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": listing(it)}],
    }


def parse(msg: object) -> str | None:
    if getattr(msg, "stop_reason", None) in ("refusal", "max_tokens"):
        return None
    text = next((b.text for b in msg.content if b.type == "text"), "")  # type: ignore[attr-defined]
    try:
        label = json.loads(text).get("label")
    except (ValueError, AttributeError):
        return None
    return label if label in LABELS else None


def pending(lake: Lake, model_id: str, limit: int) -> list[dict]:
    sql = PENDING_SQL.format(
        input_hash=INPUT_HASH,
        **{t: lake.scan(t) for t in ("snapshots", "bodies", "verdicts", "verdict_batches")},
    )
    cur = lake.connect().execute(sql, {"model": model_id, "definition": DEFINITION_VERSION})
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        r = dict(zip(cols, row, strict=True))
        if not excluded(item(r)):
            out.append(r)
            if len(out) >= limit:
                break
    return out


def submit(lake: Lake, limit: int = 100_000) -> dict:
    """Send pending product versions to Claude in one Message Batch (half price; results within a day)."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    model_id = model()
    if not model_id:
        return {"skipped": "COFFEEDATA_VERDICT_MODEL is not set"}
    todo = pending(lake, model_id, limit)
    if not todo:
        return {"pending": 0}
    reqs = [
        Request(
            custom_id=r["input_hash"],
            params=cast(MessageCreateParamsNonStreaming, request(item(r), model_id)),
        )
        for r in todo
    ]
    try:
        batch = anthropic.Anthropic().messages.batches.create(requests=reqs)
    except anthropic.APIError as e:
        log.warning("verdict batch submit failed: %s", e)
        return {"pending": len(todo), "submitted": 0, "error": str(e)}
    now = datetime.now(UTC).replace(microsecond=0)
    lake.write(
        "verdict_batches",
        f"verdict_batches/state=submitted/{batch.id}.parquet",
        [
            {"batch_id": batch.id, "state": "submitted", "at": now, "custom_id": r["input_hash"],
             "model": model_id, "definition": DEFINITION_VERSION}
            for r in todo
        ],
    )  # fmt: skip
    return {"pending": len(todo), "submitted": len(reqs), "batch_id": batch.id}


def collect(lake: Lake, run_date: str | None = None) -> dict:
    """Store the verdicts of finished batches, then mark each batch collected. Failed items stay pending."""
    import anthropic

    con = lake.connect()
    open_batches = con.execute(
        f"""select distinct batch_id, model, definition from {lake.scan("verdict_batches")} where state = 'submitted'
        and batch_id not in (select batch_id from {lake.scan("verdict_batches")} where state = 'collected')"""
    ).fetchall()
    if not open_batches:
        return {"collected": 0}
    client = anthropic.Anthropic()
    run_date = run_date or date.today().isoformat()
    stored = 0
    for batch_id, model_id, definition in open_batches:
        try:
            if client.messages.batches.retrieve(batch_id).processing_status != "ended":
                continue
            results = list(client.messages.batches.results(batch_id))
        except anthropic.APIError as e:
            log.warning("verdict batch %s: %s", batch_id, e)
            continue
        now = datetime.now(UTC).replace(microsecond=0)
        rows = [
            {"decided_at": now, "input_hash": res.custom_id, "model": model_id, "definition": definition,
             "label": label}
            for res in results
            if res.result.type == "succeeded" and (label := parse(res.result.message))
        ]  # fmt: skip
        lake.write_partition("verdicts", run_date, new_batch_id(), rows)
        lake.write(
            "verdict_batches",
            f"verdict_batches/state=collected/{batch_id}.parquet",
            [
                {
                    "batch_id": batch_id,
                    "state": "collected",
                    "at": now,
                    "model": model_id,
                    "definition": definition,
                }
            ],
        )
        stored += len(rows)
    return {"collected": stored}


def judge(rows: list[dict], model_id: str, workers: int = 8) -> dict[int, bool]:
    """Synchronous verdicts for an evaluation set: {row index: is coffee}. Excluded rows and failed calls fall
    back to the rules, exactly as the build does."""
    from concurrent.futures import ThreadPoolExecutor

    import anthropic

    from .classify import is_coffee

    client = anthropic.Anthropic()

    def one(i_r: tuple[int, dict]) -> tuple[int, bool]:
        i, r = i_r
        it = item(r)
        if excluded(it):
            return i, False
        try:
            label = parse(client.messages.create(**request(it, model_id)))
        except anthropic.APIError as e:
            log.warning("judge %d failed: %s", i, e)
            label = None
        return i, label == "coffee" if label in ("coffee", "not_coffee") else is_coffee(it)

    with ThreadPoolExecutor(workers) as pool:
        return dict(pool.map(one, enumerate(rows)))
