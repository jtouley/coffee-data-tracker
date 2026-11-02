"""Coffees per ok fetch, counted as the crawl counted them before ELT: `classify.is_coffee`, the rules alone (no
stored verdicts, not the build's listed count, no suspect-run filter). Only for fetches whose crawl recorded no
count (int_fetch_product_inputs); dim_roaster reads `coalesce(stg_fetches.coffees, this)`, so a roaster's status
is decided as it was before ELT. registry.connect_for_state counts the same way for the crawl's own selection.
"""

from collections import Counter

import pyarrow as pa

from coffeedata import classify, products

SCHEMA = pa.schema([("roaster_id", pa.string()), ("batch_id", pa.string()), ("coffees", pa.int64())])


def model(dbt, session):
    dbt.config(materialized="table")
    rows = dbt.ref("int_fetch_product_inputs").to_arrow_table().to_pylist()
    decided: dict[str, bool] = {}  # input_hash -> is coffee: each product version is classified once
    n: Counter = Counter()
    for r in rows:
        key = (r["roaster_id"], r["batch_id"])
        n[key] += 0
        h = r["input_hash"]
        if h not in decided:
            it = products.item(
                r["title"], r["product_type"], r["tags"], r["grams"], r["body_hash"], r["body_text"]
            )
            decided[h] = classify.is_coffee(it)
        n[key] += decided[h]
    return pa.Table.from_pylist(
        [{"roaster_id": rid, "batch_id": bid, "coffees": c} for (rid, bid), c in sorted(n.items())],
        schema=SCHEMA,
    )
