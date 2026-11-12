"""CLAUDE.md step 9 for #93: replay the fetch-health check's verdict on a real run (local lake copies, cut to that
run), the roasters a later crawl pass would fetch again with re-fetch on (empty or suspect), and the upper bound on
products a re-fetch could change: every distinct product those roasters had in the 35 days up to the run.

    HF_HUB_OFFLINE=1 python eval/run_quality/replay_repulls.py <out dir> <run_date> <lake root> [<lake root> ...]

The lake roots are only read: each is passed to dbt as one more entry of lake_roots, and the Lake the check needs
(its schema files) is a fresh one inside <out dir>, so nothing is written to a copy. Prints and writes
<out dir>/replay-<run_date>.json."""

import json
import sys
from pathlib import Path

import duckdb

from coffeedata import fetchhealth
from coffeedata.cli import dbt_build
from coffeedata.lake import Lake

out, rd, roots = Path(sys.argv[1]).resolve(), sys.argv[2], [str(Path(r).resolve()) for r in sys.argv[3:]]
out.mkdir(parents=True, exist_ok=True)
lake = Lake(str(out / "lake"))  # empty: only its schema files; the data is in `roots`
db = out / f"fh-{rd}.duckdb"
db.unlink(missing_ok=True)
_, results = dbt_build(
    lake,
    db,
    {"as_of_run": rd, "attempted_only": True},
    select=fetchhealth.SELECT,
    target=out / f"target-{rd}",
    extra_roots=tuple(roots),
    flags=fetchhealth.FLAGS,
)
v = fetchhealth.judge(db, results)
hook = [e for e in v["repull"] if set(e["reasons"]) - {"failed"}]  # what repull_states fetches again
ids = [e["roaster_id"] for e in hook]
snaps = [f"{r}/snapshots/*/*.parquet" for r in roots if list(Path(r).glob("snapshots/*/*.parquet"))]
prods = (
    dict(
        duckdb.connect()
        .execute(
            """select roaster_id, count(distinct product_id) from read_parquet(?, union_by_name=true)
               where roaster_id in (select unnest(?)) and try_cast(run_date as date) <= cast(? as date)
                 and try_cast(run_date as date) > cast(? as date) - interval 35 day group by 1""",
            [snaps, ids, rd, rd],
        )
        .fetchall()
    )
    if snaps and ids
    else {}
)
res = {
    "tests": v["tests"],
    "systemic": v["systemic"],
    "listed": len(v["repull"]),
    "listed_by_reason": {r: sum(r in e["reasons"] for e in v["repull"]) for r in sorted(fetchhealth.RULES)},
    "held_back": v["held_back"],
    "unattributed": len(v["unattributed"]),
    "partial": len(v["partial"]),
    "quarantined": len(v["quarantined"]),
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
}
print(rd, json.dumps(res, default=str), flush=True)
(out / f"replay-{rd}.json").write_text(json.dumps(res | {"verdict": v}, indent=1, default=str))
