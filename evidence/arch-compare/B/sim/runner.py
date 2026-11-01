"""Design B's tiny scheduler: play one simulated week of per-connector jobs and the weekly merge.

usage: runner.py SCENARIO(S1|S2) SCALE OUTDIR [PORT]
Each connector's job runs at its slot (connectors.CONNECTORS: weekday, hour) and again `retry_after_h` later (the
resume slot: the same command, which skips roasters already ok this week). The merge runs Sunday 06:00. Between
jobs the sim clock jumps ahead (nothing runs). Day 5's first job is killed at 50%. Every job gets a 240-min limit.
"""
import json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone

import duckdb
import httpx

SCEN, S, OUT = sys.argv[1], float(sys.argv[2]), os.path.abspath(sys.argv[3])
PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 8930
HERE = os.path.dirname(os.path.abspath(__file__))
WT = os.path.join(os.path.dirname(HERE), "wt")
PY = "/home/user/coffee-data-tracker/.venv/bin/python"
PROFILES = os.environ.get("PROFILES") or os.path.join(HERE, "profiles.json")
LIMIT_MIN = 240
sys.path.insert(0, os.path.join(WT, "src"))
from coffeedata.connectors import CONNECTORS, ENRICH_HOUR, MERGE_HOUR, MERGE_WEEKDAY  # noqa: E402

WEEK = datetime(2026, 11, 2, tzinfo=timezone.utc)  # Monday; run_date 2026-11-02
os.makedirs(OUT, exist_ok=True)
lake = os.path.join(OUT, "lake")
assert not os.path.exists(lake), f"{lake} exists: use a fresh OUTDIR"

slots = []
for c in CONNECTORS.values():
    t = WEEK + timedelta(days=c.weekday, hours=c.hour)
    slots.append((t, c.name, "first"))
    slots.append((t + timedelta(hours=c.retry_after_h), c.name, "resume"))
slots.append((WEEK + timedelta(days=MERGE_WEEKDAY, hours=MERGE_HOUR), "merge", "merge"))
slots.append((WEEK + timedelta(days=MERGE_WEEKDAY, hours=ENRICH_HOUR), "enrich", "enrich"))
slots.sort()

srv = subprocess.Popen(["taskset", "-c", "2,3", PY, os.path.join(HERE, "mock.py"), PROFILES, str(PORT), str(S), SCEN],
                       stdout=open(os.path.join(OUT, "mock.log"), "w"), stderr=subprocess.STDOUT)
for _ in range(50):
    try:
        httpx.get(f"http://127.0.0.1:{PORT}/__stats")
        break
    except httpx.HTTPError:
        time.sleep(0.2)
prof = {p["roaster_id"]: p["adapter"] for p in json.load(open(PROFILES))}


def stats():
    return httpx.get(f"http://127.0.0.1:{PORT}/__stats").json()


def job_fetches(name, start, end):
    """The job's own fetches rows: everything in its connector's partition (or, after a merge, the lake) it wrote."""
    part = f"{lake}/raw/connector={name}/run_date=2026-11-02"
    files = [f"{part}/fetches.parquet"] if os.path.exists(f"{part}/fetches.parquet") else []
    if os.path.isdir(f"{part}/_parts/fetches"):
        files.append(f"{part}/_parts/fetches/*.parquet")
    if not files:
        return {"roasters": 0, "ok": 0}
    con = duckdb.connect()
    r = con.execute(f"""select count(distinct roaster_id), count(distinct roaster_id) filter (where status='ok')
        from read_parquet({files}, union_by_name=true) where fetched_at >= ? and fetched_at <= ?""",
                    [start, end + timedelta(seconds=1)]).fetchone()
    return {"roasters": r[0], "ok": r[1]}


jobs = []
for t, name, kind in slots:
    httpx.post(f"http://127.0.0.1:{PORT}/__clock", params={"t": t.timestamp()})
    before = stats()
    env = dict(os.environ, SIM_SRC=os.path.join(WT, "src"), PORT=str(PORT), SCALE=str(S), SIM_T0=str(t.timestamp()),
               PROFILES=PROFILES, COFFEEDATA_LAKE=lake, PYTHONPATH=os.path.join(WT, "src"), UV_NO_SYNC="1",
               KILL_AT="0.5" if (t.weekday() == 4 and kind == "first") else "")
    for k in [k for k in env if "ANTHROPIC" in k or k.startswith(("HF_", "HUGGING"))]:
        env.pop(k)  # no Claude calls and no HF from a simulation
    args = {"merge": ["merge"], "enrich": ["enrich-week"]}.get(name, ["crawl", "--connector", name]) + \
        ["--run-date", t.date().isoformat()]
    r0 = time.monotonic()
    timed_out = False
    with open(os.path.join(OUT, f"job_{t:%a%H}_{name}_{kind}.log"), "w") as log:
        p = subprocess.Popen(["taskset", "-c", "0,1", PY, os.path.join(HERE, "jobshim.py"), *args],
                             stdout=subprocess.PIPE, stderr=log, env=env, cwd=WT, text=True)
        try:
            out, _ = p.communicate(timeout=LIMIT_MIN * 60 / S)
        except subprocess.TimeoutExpired:  # the platform's job limit: killed, like a 240-min timeout
            p.kill()
            out, _ = p.communicate()
            timed_out = True
    wall = (time.monotonic() - r0) * S / 60
    after = stats()
    d = {k: after.get(k, 0) - before.get(k, 0) for k in after if k != "refusers"}
    a = "" if name in ("merge", "enrich") else name
    end = t + timedelta(minutes=wall)
    try:
        res = json.loads(out)
    except json.JSONDecodeError:
        res = None
    rec = {"slot": f"{t:%a %Y-%m-%d %H:%M}", "job": name, "kind": kind, "exit": p.returncode, "timed_out": timed_out,
           "killed": p.returncode == -9 and not timed_out, "wall_min": round(wall, 1),
           "requests": d.get(f"{a}_req", 0) if a else 0,
           "http_429": d.get(f"{a}_429", 0) if a else 0, "http_503": d.get(f"{a}_503", 0) if a else 0,
           "mock_delta": {k: v for k, v in d.items() if v}}
    if a:
        f = job_fetches(name, t, end)
        rec |= {"attempted": f["roasters"], "ok": f["ok"],
                "ok_rate": round(f["ok"] / f["roasters"], 4) if f["roasters"] else None}
        if res:
            rec |= {"todo": res.get("todo"), "skipped_deadline": res.get("skipped_deadline"),
                    "shopify_limiter": res.get("shopify_limiter"), "deferred": res.get("deferred")}
    else:
        rec[name] = res
    jobs.append(rec)
    print(json.dumps(rec), flush=True)

final = stats()
srv.terminate()
con = duckdb.connect()
con.execute(f"create table prof as select * from read_json_auto('{PROFILES}')")
fx = f"read_parquet('{lake}/fetches/run_date=2*/*.parquet', union_by_name=true)"
con.execute(f"""create table rr as select p.roaster_id, p.adapter, count(f.roaster_id) n,
    coalesce(bool_or(f.status='ok'), false) ok, coalesce(bool_or(f.status='ok' and f.products>0), false) catalog_ok,
    arg_max(f.error, f.fetched_at) last_error
    from prof p left join {fx} f on f.roaster_id = p.roaster_id and f.run_date = date '2026-11-02' group by 1, 2""")
per = {a: {"roasters": n, "ok_share": round(ok / n, 4), "catalog_ok_share": round(cok / n, 4), "never_fetched": nf}
       for a, n, ok, cok, nf in con.execute("""select adapter, count(*), sum(ok::int), sum(catalog_ok::int),
           sum((n=0)::int) from rr group by 1 order by 1""").fetchall()}
n, ok, cok, nf, never_ok, refused = con.execute("""select count(*), sum(ok::int), sum(catalog_ok::int), sum((n=0)::int),
    sum((not ok)::int), sum((not ok and last_error like 'refused%')::int) from rr""").fetchone()


def files(glob):
    return con.execute(f"select count(*) from glob('{glob}')").fetchone()[0]


release = {t: {"files": files(f"{lake}/{t}/run_date=2026-11-02/*.parquet") if t != "enrichments"
               else files(f"{lake}/enrichments/*/run_date=2026-11-02/*.parquet"),
               "rows": con.execute(f"select count(*) from read_parquet('{lake}/{t}/*/*.parquet' )").fetchone()[0]
               if t != "enrichments" else
               con.execute(f"select count(*) from read_parquet('{lake}/enrichments/*/*/*.parquet')").fetchone()[0]}
           for t in ("run_manifests", "fetches", "snapshots", "bodies", "enrichments")}
summary = {
    "scenario": SCEN, "scale": S, "jobs": jobs,
    "all_jobs_within_limit": all(not j["timed_out"] and j["wall_min"] <= LIMIT_MIN for j in jobs),
    "end_of_week": {"roasters": n, "ok_share_7d": round(ok / n, 4), "catalog_ok_share_7d": round(cok / n, 4),
                    "per_connector": per, "refused": refused, "never_fetched": nf, "never_ok": never_ok},
    "release": release, "raw_files_left": files(f"{lake}/raw/**/*.parquet"),
    "mock_totals": final,
}
json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)
print(json.dumps(summary["end_of_week"]), json.dumps(release), summary["all_jobs_within_limit"])
