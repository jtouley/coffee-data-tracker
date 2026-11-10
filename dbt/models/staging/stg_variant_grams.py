"""Each stored product version's bag weights, one per variant in the snapshot's order, from the current rules.

The crawler reads a variant's grams from its option's title only, so a weight the store gives in the product title,
in a slug-style option ("1-lb-custom-whole-bean") or in the description is lost, and a misread ("1/2 lb" as 907 g)
is kept. coffeedata.bag_weight reads the stored text again (pure: the same lake gives the same grams), and a fix to
the rules applies to every past week without fetching anything again. dim_coffee prices bags with these grams.
"""

import pyarrow as pa

from coffeedata import bag_weight

SCHEMA = pa.schema([("grams_key", pa.string()), ("grams", pa.list_(pa.int32()))])


def row(r: dict) -> dict:
    options = [(v["title"], v["price"], v["grams"]) for v in r["variants"] or []]
    return {
        "grams_key": r["grams_key"],
        "grams": bag_weight.product_grams(r["title"], r["body_text"], options),
    }


def model(dbt, session):
    dbt.config(materialized="table")
    inputs = dbt.ref("stg_variant_grams_inputs").to_arrow_table()
    return pa.Table.from_pylist([row(r) for r in inputs.to_pylist()], schema=SCHEMA)
