"""Aggregate counts from each tree's build of the lake copy and its site. Prints totals only."""

import json
import re
import sys
from pathlib import Path

import duckdb

E = sys.argv[1]
out = {}


def one(con, sql):
    return con.execute(sql).fetchone()


for t in ("main", "a1733aa", "fix"):
    con = duckdb.connect(f"{E}/trees/{t}/coffee.duckdb", read_only=True)
    m = {}
    m["dim_coffee rows / listed / flagged wholesale (listed)"] = one(
        con,
        "select count(*), count(*) filter (where is_listed), count(*) filter (where is_listed and is_wholesale) from dim_coffee",
    )
    m["dim_roaster sum listed / in stock / archived"] = one(
        con, "select sum(coffees_listed), sum(coffees_in_stock), sum(coffees_archived) from dim_roaster"
    )
    m["mart_market coffees (sum)"] = one(con, "select sum(coffees) from mart_market")
    m["mart_new_arrivals rows"] = one(con, "select count(*) from mart_new_arrivals")
    m["public_market_weekly coffees (sum)"] = one(con, "select sum(coffees) from public_market_weekly")
    m["mart_build_profile listed / in stock / roasters"] = tuple(
        dict(con.execute("select metric, value from mart_build_profile").fetchall())[k]
        for k in ("listed_coffees", "in_stock_coffees", "roasters_with_coffee")
    )
    html = " ".join(Path(f"{E}/trees/{t}/site/index.html").read_text().split())
    wk = re.search(
        r"<b>([\d,]+)</b> new arrivals · <b>([\d,]+)</b> price changes · <b>([\d,]+)</b> delisted", html
    )
    m["site week: new / price changes / delisted"] = wk.groups() if wk else None
    cat = json.loads(Path(f"{E}/trees/{t}/site/catalog/catalog.json").read_text())
    m["site catalog rows"] = len(cat["rows"])
    out[t] = m
for k in out["main"]:
    print(f"{k}: " + " | ".join(f"{t} {out[t][k]}" for t in out))
