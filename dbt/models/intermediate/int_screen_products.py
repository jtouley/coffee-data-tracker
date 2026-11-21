"""What each product on a screen's page says, from the current rules (docs/design/roaster-screen.md, section 2.5).

One row per product a screen read (stg_screen_products): whether the rules call it coffee (products.decide with no
verdict), whether it is named as coffee, wholesale, its roast level and origin (extract.rules), its bag sizes
(bag_weight), and whether its vendor is the store's own name or another roaster the registry knows. All of it is
coffeedata.screen_rules.product_signals, a pure function of the stored row, the place's name (stg_screen_places) and
the registry's names (stg_registry): a rules change re-scores every past screen without a request.
"""

import pyarrow as pa

from coffeedata import screen_rules

SCHEMA = pa.schema(
    [
        ("screen_id", pa.string()),
        ("roaster_id", pa.string()),
        ("product_id", pa.string()),
        ("is_coffee", pa.bool_()),
        ("named", pa.bool_()),
        ("is_wholesale", pa.bool_()),
        ("roast_level", pa.string()),
        ("origin_country", pa.string()),
        ("is_blend", pa.bool_()),
        ("bag_grams", pa.list_(pa.int32())),
        ("vendor_key", pa.string()),
        ("own_vendor", pa.bool_()),
        ("other_roaster", pa.bool_()),
        ("currencies", pa.list_(pa.string())),
    ]
)


def model(dbt, session):
    dbt.config(materialized="table")
    rows = dbt.ref("stg_screen_products").to_arrow_table().to_pylist()
    places = dbt.ref("stg_screen_places").to_arrow_table().to_pylist()
    names = {p["roaster_id"]: p["name"] for p in places}
    registry = dbt.ref("stg_registry").to_arrow_table().to_pylist()
    known = screen_rules.KnownRoasters(sorted(r["roaster_name"] for r in registry if r["roaster_name"]))
    out = []
    for r in sorted(rows, key=lambda r: (r["screen_id"], r["product_id"])):
        rid = r["roaster_id"]
        signals = screen_rules.product_signals(r, names.get(rid) or rid, rid, known)
        out.append({"screen_id": r["screen_id"], "roaster_id": rid, "product_id": r["product_id"], **signals})
    return pa.Table.from_pylist(out, schema=SCHEMA)
