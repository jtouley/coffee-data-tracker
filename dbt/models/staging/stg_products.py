"""Whether each product version is coffee, and its rule-based attributes, from the current code.

The snapshots keep every product a store lists, coffee or not, and the bodies keep each description, so this is
a pure function of the raw lake and `coffeedata.classify` / `coffeedata.extract`. Every build gives the same
result from the same lake, and a fix to the rules applies to every past week without fetching anything again.
"""

import pyarrow as pa

from coffeedata import classify, extract
from coffeedata.models import CatalogItem, Variant

SCHEMA = pa.schema(
    [
        ("input_hash", pa.string()),
        ("is_coffee", pa.bool_()),
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
ATTRS = [f.name for f in SCHEMA][4:]


def row(r: dict) -> dict:
    item = CatalogItem(
        roaster_id="",
        product_id="",
        handle="",
        url="",
        title=r["title"] or "",
        platform="",
        product_type=r["product_type"] or "",
        tags=r["tags"] or [],
        variants=[Variant(variant_id=str(i), grams=g) for i, g in enumerate(r["grams"] or [])],
        body_hash=r["body_hash"] or "",
        body_text=r["body_text"] or "",
    )
    out = {
        "input_hash": r["input_hash"],
        "is_coffee": classify.is_coffee(item),
        "is_wholesale": classify.is_wholesale(item),
        "clean_title": item.title,
    }
    e = extract.rules(item) if out["is_coffee"] else None
    return out | {a: getattr(e, a) if e else None for a in ATTRS}


def model(dbt, session):
    dbt.config(materialized="table")
    inputs = dbt.ref("stg_product_inputs").to_arrow_table()
    return pa.Table.from_pylist([row(r) for r in inputs.to_pylist()], schema=SCHEMA)
