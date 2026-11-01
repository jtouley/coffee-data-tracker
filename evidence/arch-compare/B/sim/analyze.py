"""Score one simulated week from the merged lake: per-job ok rates (rows by fetched_at in the job's window) and
end-of-week coverage. usage: analyze.py SCENARIO JOBS_JSONL LAKE OUT.json"""
import json, os, sys
from datetime import datetime, timedelta, timezone

import duckdb

SCEN, JOBS, LAKE, OUT = sys.argv[1:5]
PROFILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
jobs = [json.loads(l) for l in open(JOBS) if l.startswith('{"slot"')]
con = duckdb.connect()
con.execute(f"create table prof as select * from read_json_auto('{PROFILES}')")
con.execute(f"create table f as select * from read_parquet('{LAKE}/fetches/run_date=2*/*.parquet', union_by_name=true)")
for j in jobs:
    if j["job"] in ("merge", "enrich"):
        continue
    t = datetime.strptime(j["slot"][4:], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    end = t + timedelta(minutes=j["wall_min"], seconds=2)
    n, ok, rows = con.execute("""select count(distinct roaster_id), count(distinct roaster_id) filter (where status='ok'),
        count(*) from f where adapter = ? and fetched_at between ? and ?""", [j["job"], t, end]).fetchone()
    j |= {"roasters_written": n, "ok": ok, "ok_rate": round(ok / n, 4) if n else None, "fetch_rows": rows}
con.execute("""create table rr as select p.roaster_id, p.adapter, count(f.roaster_id) n,
    coalesce(bool_or(f.status='ok'), false) ok, coalesce(bool_or(f.status='ok' and f.products>0), false) catalog_ok,
    arg_max(f.error, f.fetched_at) last_error
    from prof p left join f on f.roaster_id = p.roaster_id group by 1, 2""")
per = {a: {"roasters": n, "ok_share": round(ok / n, 4), "catalog_ok_share": round(cok / n, 4), "never_fetched": nf,
           "refused": rf}
       for a, n, ok, cok, nf, rf in con.execute("""select adapter, count(*), sum(ok::int), sum(catalog_ok::int),
           sum((n=0)::int), sum((not ok and last_error like 'refused%')::int) from rr group by 1 order by 1""").fetchall()}
n, ok, cok, nf, never_ok, refused = con.execute("""select count(*), sum(ok::int), sum(catalog_ok::int), sum((n=0)::int),
    sum((not ok)::int), sum((not ok and last_error like 'refused%')::int) from rr""").fetchone()


def count(glob):
    return con.execute(f"select count(*) from glob('{glob}')").fetchone()[0]


release = {}
for t in ("run_manifests", "fetches", "snapshots", "bodies", "enrichments"):
    g = f"{LAKE}/enrichments/*/run_date=2026-11-02/*.parquet" if t == "enrichments" else f"{LAKE}/{t}/run_date=2026-11-02/*.parquet"
    nf_ = count(g)
    release[t] = {"files": nf_, "rows": con.execute(f"select count(*) from read_parquet('{g}')").fetchone()[0] if nf_ else 0}
merge = next(j for j in jobs if j["job"] == "merge")
summary = {
    "scenario": SCEN, "jobs": jobs,
    "enrich_job": next((j.get("enrich") for j in jobs if j["job"] == "enrich"), None),
    "all_jobs_within_limit": all(not j["timed_out"] and j["wall_min"] <= 240 for j in jobs),
    "end_of_week": {"roasters": n, "ok_share_7d": round(ok / n, 4), "catalog_ok_share_7d": round(cok / n, 4),
                    "per_connector": per, "refused": refused, "never_fetched": nf, "never_ok": never_ok},
    "release": {"run_date": "2026-11-02", "tables": release, "merge": merge.get("merge"),
                "raw_parquet_left_after_merge": count(f"{LAKE}/raw/**/*.parquet"),
                "rollup_manifests": count(f"{LAKE}/raw/_manifests/*.json")},
}
json.dump(summary, open(OUT, "w"), indent=1, default=str)
print(json.dumps(summary["end_of_week"]), json.dumps(release), summary["all_jobs_within_limit"])
