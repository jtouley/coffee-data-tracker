"""#4 cross-table reconciliation, replayed over the recorded runs (eval/dq/README.md).

    python recon_eval.py items <replay-dir> <tag> <data-dir>   # every disagreement in every replayed build
    python recon_eval.py score <data-dir> <labels-dir> <set>

Checks, per build (the same SQL as dbt/tests/public_tables_reconcile.sql):
  roaster_counts     public_roaster.coffees_listed / coffees_in_stock against public_coffee's rows per roaster
  listed_total       sum(dim_roaster.coffees_listed) against mart_build_profile.listed_coffees
  event_roaster      a public price event's roaster_id against its public_coffee row's
  orphan_event       a fct_price_event whose coffee isn't in dim_coffee; main's relationships test (warn) reports
                     every one, the change only those whose product's latest record is still classified as coffee
                     (a reclassified product leaves dim_coffee by design and keeps its events)
Every disagreement any build has is an item (an orphan item is one coffee, with all its events).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

from common import SETS, fmt, reference, split_of

RUNS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-30", "2026-11-02"]

CHECKS = {
    "roaster_counts": """
        select r.roaster_id as key, r.roaster_id,
               format('listed {} / {}, in stock {} / {}', r.coffees_listed, coalesce(c.listed, 0),
                      r.coffees_in_stock, coalesce(c.in_stock, 0)) as detail, null::boolean as explained
        from public_roaster r left join (
            select roaster_id, count(*) filter (where is_listed and catalog_status <> 'archived') as listed,
                   count(*) filter (where in_stock) as in_stock
            from public_coffee where not is_wholesale group by roaster_id) c using (roaster_id)
        where r.coffees_listed <> coalesce(c.listed, 0) or r.coffees_in_stock <> coalesce(c.in_stock, 0)""",
    "listed_total": """
        select 'listed_total' as key, null as roaster_id, format('{} / {}', d, m) as detail, null::boolean
        from (select sum(coffees_listed) as d from dim_roaster),
             (select value as m from mart_build_profile where metric = 'listed_coffees') where d <> m""",
    "event_roaster": """
        select e.coffee_id as key, e.roaster_id, format('{} vs {}', e.roaster_id, c.roaster_id), null::boolean
        from public_price_event e join public_coffee c using (coffee_id) where e.roaster_id <> c.roaster_id""",
    "orphan_event": """
        with o as (
            select coffee_id, any_value(roaster_id) as roaster_id, count(*) as events
            from fct_price_event e where not exists (select 1 from dim_coffee d where d.coffee_id = e.coffee_id)
            group by coffee_id
        ),
        l as (
            select s.coffee_id, p.is_coffee, s.title from stg_snapshots s join stg_products p using (input_hash)
            qualify row_number() over (partition by s.coffee_id order by s.run_date desc) = 1
        )
        select o.coffee_id, o.roaster_id, format('{} events; latest title {}', o.events, l.title),
               coalesce(l.is_coffee = false, false) as explained
        from o left join l using (coffee_id)""",
}


def cmd_items(replay: Path, tag: str, data: Path) -> None:
    items, inputs = [], []
    for run in RUNS:
        con = duckdb.connect(str(replay / f"coffee-{tag}-{run}.duckdb"), read_only=True)
        for check, sql in CHECKS.items():
            for key, roaster, detail, explained in con.execute(sql).fetchall():
                iid = f"{run}|{check}|{key}"
                items.append({"id": iid, "run": run, "check": check, "split": split_of(roaster or key),
                              "explained": explained})
                inputs.append({"id": iid, "evidence": evidence(con, check, key, detail)})
    data.mkdir(parents=True, exist_ok=True)
    (data / "recon_items.json").write_text(json.dumps(items, indent=1))
    (data / "recon_inputs.json").write_text(json.dumps(inputs, indent=1))
    by = {}
    for i in items:
        by[(i["run"], i["check"])] = by.get((i["run"], i["check"]), 0) + 1
    print(len(items), "disagreements", by)


def evidence(con: duckdb.DuckDBPyConnection, check: str, key: str, detail: str) -> str:
    if check != "orphan_event":
        return f"Check {check}: {key}: {detail}"
    hist = con.execute(
        """select s.run_date, s.title, s.product_type, p.is_coffee from stg_snapshots s
           join stg_products p using (input_hash) where s.coffee_id = ? order by s.run_date""",
        [key],
    ).fetchall()
    events = con.execute(
        "select run_date, event, price_usd, previous_price_usd from fct_price_event where coffee_id = ? order by 1",
        [key],
    ).fetchall()
    return "\n".join([
        f"Price events for product {key} (in the internal price-event table), whose product is missing from the "
        "coffee table (dim_coffee, and so public_coffee):",
        *[f"  - {r[0]} {r[1]} price {r[2]} previous {r[3]}" for r in events[:8]],
        "The product's records, per run (title | product type | classified as coffee by the current rules):",
        *[f"  - {r[0]} | {r[1]} | {r[2]} | {r[3]}" for r in hist],
        "Stated definition: the coffee table keeps a product only if its latest record is classified as coffee; "
        "price events are kept for every product that was coffee when they happened.",
    ])


def cmd_score(data: Path, labels: Path, which: str) -> None:
    items = [i for i in json.loads((data / "recon_items.json").read_text()) if which in ("all", i["split"])]
    ref = reference(labels, "recon") if (labels / "recon_A.json").exists() else {}
    for name, flag in (("main", lambda i: i["check"] == "orphan_event"), ("change", lambda i: not i["explained"])):
        flagged = [i for i in items if flag(i)]
        wrong = sum(ref.get(i["id"]) == "by_design" for i in flagged)
        defects = [i for i in items if ref.get(i["id"]) == "defect"]
        print(f"[{which}] {name}: {len(items)} disagreements, flagged {len(flagged)}; "
              f"confidently wrong (by design but flagged) {fmt(wrong, len(flagged))}; "
              f"defects caught {fmt(sum(flag(i) for i in defects), len(defects))}")


def main() -> None:
    if sys.argv[1] == "items":
        cmd_items(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    else:
        cmd_score(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])


if __name__ == "__main__":
    main()
