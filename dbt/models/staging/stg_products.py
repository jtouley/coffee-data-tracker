"""Whether each product version is coffee, and its rule-based attributes, from the current code and stored verdicts.

The snapshots keep every product a store lists, coffee or not, and the bodies keep each description, so this is
a pure function of the raw lake and `coffeedata.classify` / `coffeedata.extract`. Where Claude has a stored
verdict for the version (verdict.py, stg_verdicts) it decides coffee or not; the rules decide the rest, and the
rules' outright exclusions always hold. Every build gives the same
result from the same lake, and a fix to the rules applies to every past week without fetching anything again.
"""

import pyarrow as pa

from coffeedata import products

SCHEMA = pa.schema(
    [
        ("input_hash", pa.string()),
        ("is_coffee", pa.bool_()),
        ("classified_by", pa.string()),
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
assert [f.name for f in SCHEMA][5:] == products.ATTRS


def row(r: dict, verdicts: dict[str, str]) -> dict:
    """The logic lives in coffeedata.products (shared with registry.STATE_SQL and enrich.pending)."""
    return products.derive(r, verdicts)[0]


def model(dbt, session):
    dbt.config(materialized="table")
    inputs = dbt.ref("stg_product_inputs").to_arrow_table()
    verdicts = {v["input_hash"]: v["label"] for v in dbt.ref("stg_verdicts").to_arrow_table().to_pylist()}
    return pa.Table.from_pylist([row(r, verdicts) for r in inputs.to_pylist()], schema=SCHEMA)
