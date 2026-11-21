"""What each screened home page says about roasting, from the current rules (section 4.1's roast_words and
serves_phrase).

One row per screen that read a home page (stg_screens): the first roastery wording ("we roast in house") and the
first wording that the place serves another roaster's coffee ("proudly serving ..."), by
coffeedata.screen_rules.home_signals. Only the home page's own text is read, never product text, which a reseller
copies from the roaster.
"""

import pyarrow as pa

from coffeedata import screen_rules

SCHEMA = pa.schema(
    [("screen_id", pa.string()), ("roast_phrase", pa.string()), ("serves_phrase", pa.string())]
)


def model(dbt, session):
    dbt.config(materialized="table")
    screens = dbt.ref("stg_screens").to_arrow_table().to_pylist()
    screens = sorted((s for s in screens if s["home_text"] is not None), key=lambda s: s["screen_id"])
    places = dbt.ref("stg_screen_places").to_arrow_table().to_pylist()
    names = {p["roaster_id"]: p["name"] for p in places}
    registry = dbt.ref("stg_registry").to_arrow_table().to_pylist()
    known = screen_rules.KnownRoasters(sorted(r["roaster_name"] for r in registry if r["roaster_name"]))
    out = [
        {
            "screen_id": s["screen_id"],
            **screen_rules.home_signals(s["home_text"], names.get(s["roaster_id"]) or s["roaster_id"], known),
        }
        for s in screens
    ]
    return pa.Table.from_pylist(out, schema=SCHEMA)
