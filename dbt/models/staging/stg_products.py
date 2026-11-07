"""Whether each product version is coffee, and its rule-based attributes, from the current code and stored verdicts.

The snapshots keep every product a store lists, coffee or not, and the bodies keep each description, so this is
a pure function of the raw lake and `coffeedata.classify` / `coffeedata.extract`. Where Claude has a stored
verdict for the version (verdict.py, stg_verdicts) it decides coffee or not; the rules decide the rest, and the
rules' outright exclusions always hold. Every build gives the same
result from the same lake, and a fix to the rules applies to every past week without fetching anything again.
"""

import pyarrow as pa

from coffeedata import classify, extract, products

SCHEMA = pa.schema(
    [
        ("input_hash", pa.string()),
        ("is_coffee", pa.bool_()),
        ("classified_by", pa.string()),
        ("verdict_model", pa.string()),
        # Why: verdict:<label>, or the rule that decided (products.decide_why). Only for the owner's view
        # (product_decisions); nothing public reads it.
        ("reason", pa.string()),
        ("is_wholesale", pa.bool_()),
        ("clean_title", pa.string()),
        ("origin_country", pa.string()),
        ("origin_region", pa.string()),
        ("producer", pa.string()),
        ("process", pa.string()),
        ("varietals", pa.list_(pa.string())),
        ("elevation_masl_min", pa.int32()),
        ("elevation_masl_max", pa.int32()),
        ("roast_level", pa.string()),
        ("tasting_notes", pa.list_(pa.string())),
        ("is_blend", pa.bool_()),
        ("is_decaf", pa.bool_()),
    ]
)
# The rule attributes, after the decision's columns.
ATTRS = SCHEMA.names[SCHEMA.names.index("clean_title") + 1 :]


def row(r: dict, verdicts: dict[str, tuple[str, str]]) -> dict:
    # The decisions live in coffeedata.products, shared with the enrichment job and the registry.
    item = products.item(r["title"], r["product_type"], r["tags"], r["grams"], r["body_hash"], r["body_text"])
    label, verdict_model = verdicts.get(r["input_hash"], (None, None))
    is_coffee, classified_by, reason = products.decide_why(item, label)
    out = {
        "input_hash": r["input_hash"],
        "is_coffee": is_coffee,
        "classified_by": classified_by,
        "verdict_model": verdict_model if classified_by == "claude" else None,
        "reason": reason,
        "is_wholesale": classify.is_wholesale(item),
        "clean_title": item.title,
    }
    e = extract.rules(item) if out["is_coffee"] else None
    return out | {a: getattr(e, a) if e else None for a in ATTRS}


def model(dbt, session):
    dbt.config(materialized="table")
    inputs = dbt.ref("stg_product_inputs").to_arrow_table()
    verdicts = {
        v["input_hash"]: (v["label"], v["model"])
        for v in dbt.ref("stg_verdicts").to_arrow_table().to_pylist()
    }
    return pa.Table.from_pylist([row(r, verdicts) for r in inputs.to_pylist()], schema=SCHEMA)
