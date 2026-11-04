"""Product versions where the Python readers of the lake, which read raw tags, would decide differently from the
build, which reads decoded tags (decode_html_entities). One row per disagreeing (roaster, product, version).

registry.py counts a roaster's coffees with `classify.is_coffee`; enrich.py picks Claude's candidates with
`products.needs_claude` / `products.rules` (which apply `classify.excluded` to a stored verdict); verdict.py keys
verdicts by input_hash, over the raw tags, so they join either way. This runs those decisions over both forms of
each version int_escaped_tag_inputs lists: coffee or not by the rules, excluded or not, and the rule attributes
with no verdict and with a "coffee" verdict (a "not_coffee" verdict gives no attributes either way). The prompt
text sent to Claude differs whenever a tag is decoded, by design, so it isn't compared.

The data test python_readers_agree_on_tags fails the build on any row; `coffeedata build` prints them.
"""

import pyarrow as pa

from coffeedata import classify, products

SCHEMA = pa.schema(
    [
        ("roaster_id", pa.string()),
        ("product_id", pa.string()),
        ("input_hash", pa.string()),
        ("title", pa.string()),
        ("raw_tags", pa.list_(pa.string())),
        ("tags", pa.list_(pa.string())),
        ("differs", pa.list_(pa.string())),
    ]
)


def decisions(r: dict, tags) -> dict:
    """The tag-driven outputs the Python readers compute for one version."""
    it = products.item(r["title"], r["product_type"], tags, r["grams"], r["body_hash"], r["body_text"])
    return {
        "is_coffee": classify.is_coffee(it),
        "excluded": classify.excluded(it),
        "rules": products.rules(it, None),
        "rules_with_coffee_verdict": products.rules(it, "coffee"),
    }


def differs(r: dict) -> list[str]:
    raw, decoded = decisions(r, r["raw_tags"]), decisions(r, r["tags"])
    return [k for k in raw if raw[k] != decoded[k]]


def model(dbt, session):
    dbt.config(materialized="table")
    rows = dbt.ref("int_escaped_tag_inputs").to_arrow_table().to_pylist()
    found: dict[str, list[str]] = {}  # input_hash -> outputs that differ: each version is decided once
    out = []
    for r in rows:
        if r["input_hash"] not in found:
            found[r["input_hash"]] = differs(r)
        if d := found[r["input_hash"]]:
            out.append({k: r[k] for k in ("roaster_id", "product_id", "input_hash", "title", "raw_tags", "tags")}
                       | {"differs": d})  # fmt: skip
    return pa.Table.from_pylist(out, schema=SCHEMA)
