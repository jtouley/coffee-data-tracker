"""Design A's metrics from the simulated weeks (runs/*/week.json and their local lakes) -> ../result.json"""
import json, math, os, sys
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
prof = {p["roaster_id"]: p for p in json.load(open(os.path.join(HERE, "profiles.json")))}


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def share(k, n):
    return {"k": k, "n": n, "share": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


weeks = {}
RUNS = os.environ.get("RUNS", "runs")
for d in sorted(os.listdir(os.path.join(HERE, RUNS))):
    f = os.path.join(HERE, RUNS, d, "week.json")
    if not os.path.exists(f):
        continue
    w = json.load(open(f))
    con = duckdb.connect()
    con.execute("create table prof as select * from read_json_auto(?)", [os.path.join(HERE, "profiles.json")])
    con.execute(f"create view fx as select * from read_parquet('{w['lake']}/fetches/*/*.parquet', union_by_name=true) where roaster_id is not null")
    con.execute(f"create view mf as select * from read_parquet('{w['lake']}/run_manifests/*/*.parquet', union_by_name=true) where roaster_id is not null")
    jobs = []
    for j in w["jobs"]:
        lab = j["label"]
        rows = con.execute("""select adapter, count(distinct roaster_id), count(distinct roaster_id) filter (where status='ok'),
                 count(*), sum(requests), sum(coalesce(list_sum([s.responses for s in http_statuses if s.status=429]),0)),
                 sum(coalesce(list_sum([s.responses for s in http_statuses if s.status=503]),0))
                 from fx where code_version=? group by 1 order by 1""", [lab]).fetchall()
        per = {a: {"roasters": n, "ok": share(ok, n), "fetch_rows": r, "requests": int(q or 0), "429s": int(t or 0),
                   "503s": int(f5 or 0)} for a, n, ok, r, q, t, f5 in rows}
        jobs.append({
            "job": lab, "day": j.get("today"), "skipped": j.get("skipped"), "killed_at": j.get("killed"),
            "wall_min": round(j.get("wall_min") or 0, 1),
            "main_pass_min": round(j["main_wall_min"], 1) if j.get("main_wall_min") else None,
            "inside_240_min": (j.get("wall_min") or 0) <= 240,
            "stopped_at_deadline": (j.get("main") or {}).get("stopped_at_deadline"),
            "requests": sum(p["requests"] for p in per.values()),
            "429s": sum(p["429s"] for p in per.values()),
            "server": j.get("server"), "shopify_limiter": (j.get("main") or {}).get("shopify_limiter"),
            "deferred": (j.get("main") or {}).get("deferred"), "per_connector": per,
        })
    # End of week: every row is inside the 7 days. A successful fetch: status ok.
    eow = con.execute("""with r as (select p.roaster_id, p.adapter, p.products>0 has_cat,
            bool_or(f.status='ok') ok, bool_or(f.status='ok' and f.products>0) cat_ok, count(f.roaster_id) n,
            arg_max(f.error, f.fetched_at) last_err
            from prof p left join fx f using (roaster_id) group by all)
        select adapter, count(*), sum(ok::int), sum((n=0)::int), sum(coalesce(last_err like 'refused:%', false)::int),
               sum(has_cat::int), sum((has_cat and cat_ok)::int) from r group by rollup(adapter) order by 1 nulls last""").fetchall()
    eowd = {}
    for a, n, ok, never, refused, hc, cok in eow:
        eowd[a or "all"] = {"roasters": n, "fetched_ok_7d": share(int(ok), n), "never_fetched": int(never),
                            "refused": int(refused), "catalog_ok_of_has_catalog": share(int(cok), int(hc))}
    # The weekly release: does the run pass the gate's completeness floors? (dbt's run_is_complete, run_ok_rate,
    # adapter_ok_rate, approximated here in SQL over the same raw tables; has_catalog ~ profile products > 0.)
    rd = con.execute("select max(run_date) from mf where mode='full'").fetchone()[0]
    exp, att = con.execute("""select count(distinct m.roaster_id), count(distinct f.roaster_id)
        from mf m left join fx f on f.roaster_id=m.roaster_id and f.run_date=m.run_date where m.run_date=? and m.mode='full'""", [rd]).fetchone()
    runs = [r[0] for r in con.execute("select distinct run_date from fx order by 1").fetchall()]
    ad = con.execute("""select p.adapter, count(*), sum(coalesce(c.ok,false)::int) from prof p left join
        (select roaster_id, bool_or(status='ok' and products>0) ok from fx where run_date=? group by 1) c using (roaster_id)
        where p.products>0 group by 1 order by 1""", [rd]).fetchall()
    tot_n, tot_ok = sum(r[1] for r in ad), sum(r[2] for r in ad)
    gate = {"run_date": str(rd), "run_dates_in_lake": [str(r) for r in runs],
            "attempted": share(att, exp), "run_is_complete": att == exp,
            "ok_share_has_catalog": share(tot_ok, tot_n), "run_ok_rate_pass": tot_ok / tot_n >= 0.9,
            "adapter_ok": {a: share(ok, n) for a, n, ok in ad},
            "adapter_ok_rate_pass": all(ok / n >= 0.5 for a, n, ok in ad if n >= 20)}
    gate["release"] = (f"run {rd} served (all {att} roasters attempted, one run_date)" if gate["run_is_complete"]
                       and gate["run_ok_rate_pass"] and gate["adapter_ok_rate_pass"] else
                       "no new release: the gate fails and the previous release stays")
    weeks[d] = {"scenario": w["scenario"], "placement": w["placement"], "scale": w["scale"], "jobs": jobs,
                "all_jobs_inside_limit": all(j["inside_240_min"] for j in jobs), "end_of_week": eowd, "release": gate}
json.dump(weeks, sys.stdout, indent=1, default=str)
