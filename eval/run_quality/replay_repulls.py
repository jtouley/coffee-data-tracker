"""CLAUDE.md step 9 for #93: replay crawl-dq's verdict on a real run (a local lake copy, cut to that run), the
roasters a later crawl pass would fetch again (empty or suspect), and the upper bound on products a repull could
change: every distinct product those roasters had in the 35 days up to the run. Prints and writes replay.json."""

import json
import sys
from pathlib import Path

import duckdb

from coffeedata import crawldq
from coffeedata.cli import dbt_build
from coffeedata.lake import Lake

root, out = Path(sys.argv[1]), Path(sys.argv[2])
lake = Lake(str(root))
res = {}
for rd in sys.argv[3:]:
    db = out / f"dq-{rd}.duckdb"
    db.unlink(missing_ok=True)
    _, results = dbt_build(
        lake,
        db,
        {"as_of_run": rd, "attempted_only": True},
        select=crawldq.SELECT,
        target=out / f"target-{rd}",
        flags=crawldq.FLAGS,
    )
    v = crawldq.judge(db, results)
    hook = [e for e in v["repull"] if set(e["reasons"]) - {"failed"}]  # what repull_states fetches again
    ids = [e["roaster_id"] for e in hook]
    c = duckdb.connect()
    prods = dict(
        c.execute(
            f"""
        select roaster_id, count(distinct product_id) from read_parquet('{root}/snapshots/*/*.parquet',
            union_by_name=true) where roaster_id in (select unnest(?)) and try_cast(run_date as date) <= cast(? as date)
            and try_cast(run_date as date) > cast(? as date) - interval 35 day group by 1""",
            [ids, rd, rd],
        ).fetchall()
    )
    res[rd] = {
        "systemic": [(s["scope"], s.get("connector") or s.get("connectors")) for s in v["systemic"]],
        "listed": len(v["repull"]),
        "held_back": v["held_back"],
        "unattributed": len(v["unattributed"]),
        "hook": [
            (
                e["roaster_id"],
                e["adapter"],
                e["reasons"],
                e.get("products"),
                e.get("baseline_products"),
                prods.get(e["roaster_id"], 0),
            )
            for e in hook
        ],
        "upper_bound_products": sum(prods.values()),
        "baseline_sum": sum(max(e.get("products") or 0, e.get("baseline_products") or 0) for e in hook),
    }
    print(rd, json.dumps(res[rd], default=str), flush=True)
(out / "replay.json").write_text(json.dumps(res, indent=1, default=str))
