"""Design C's result.json from the simulated weeks (runner.py output dirs).

usage: report.py OUT.json S1_DIR S2_DIR
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

OUT, DIRS = sys.argv[1], {"S1": Path(sys.argv[2]), "S2": Path(sys.argv[3])}


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]


def scenario(d: Path) -> dict:
    week = json.loads((d / "week.json").read_text())
    ctrl = [r for p in sorted((d / "bucket/control").rglob("*.parquet")) for r in pq.read_table(p).to_pylist()]
    by_job = defaultdict(list)
    for r in ctrl:
        by_job[r["job_id"]].append(r)
    jobs = []
    for j in week["jobs"]:
        res = j["result"]
        rows = []
        if j["step"] == "connector" and res.get("job_id"):
            rows = by_job[res["job_id"]]
        elif j["step"] == "sweep":
            rows = [r for jid, rs in by_job.items() if jid.startswith(f"sweep-{j['run_date']}-") for r in rs]
        ok = defaultdict(Counter)
        for r in rows:
            ok[r["connector"]][r["outcome"]] += 1
        jobs.append(
            {
                "job": j["job"],
                "sim_start_h": j["sim_start_h"],
                "wall_min": j["wall_min"],
                "finished": j["finished"],
                "killed_at_50pct": j["killed_at_50pct"],
                "within_240_min": j["wall_min"] <= 240,
                "requests": j["requests"],
                "http_429": j["http_429"],
                "ip_wide_429": j["ip_wide_429"],
                "woo_503": j["woo_503"],
                "ok_rate_per_connector": {
                    c: {"ok": n["ok"], "of": sum(n.values()), "pct": round(100 * n["ok"] / sum(n.values()), 1),
                        "outcomes": dict(n)} for c, n in ok.items()
                },
                **({"resumed_skipped": res.get("resumed_skipped"), "todo": res.get("todo")}
                   if "resume" in j["job"] or "killed" in j["job"] else {}),
                **({"waves": res.get("waves")} if j["step"] == "sweep" else {}),
                **({"routed_to_html_stub": res.get("routed")} if j["step"] == "html" else {}),
            }
        )
    merge = next(j["result"] for j in week["jobs"] if j["step"] == "merge")
    lake = d / "lake"
    con = duckdb.connect()
    scan = lambda t: f"read_parquet('{lake}/{t}/*/*.parquet', union_by_name=true)"  # noqa: E731
    per = con.execute(f"""
        select m.adapter, count(*), count(f.roaster_id), sum((f.status = 'ok')::int)
        from {scan('run_manifests')} m left join {scan('fetches')} f using (roaster_id, run_date)
        where m.mode = 'full' group by 1 order by 1""").fetchall()
    tot = [sum(x[i] or 0 for x in per) for i in (1, 2, 3)]
    refused = {r["roaster_id"] for r in ctrl if r["outcome"] == "refused"}
    files = lambda root, pat: sorted(str(p.relative_to(root)) for p in root.rglob(pat) if "schema" not in p.name)  # noqa: E731
    raw_files = files(d / "bucket", "raw/**/*.parquet")
    return {
        "jobs": jobs,
        "all_jobs_finished_inside_limit": all(j["within_240_min"] for j in jobs),
        "end_of_week": {
            "roasters_expected": tot[0],
            "ok_in_last_7_days": {"n": tot[2], "pct": round(100 * tot[2] / tot[0], 2), "wilson95": wilson(tot[2], tot[0])},
            "per_connector": {a: {"expected": n, "ok": ok, "pct": round(100 * ok / n, 1), "wilson95": wilson(ok, n)}
                              for a, n, _, ok in per},
            "refused_stores": len(refused),
            "never_fetched": merge["never_fetched"],
            "attempted_not_ok": tot[1] - tot[2],
        },
        "weekly_release": {
            "run_date": "2026-11-08",
            "lake_files_written": files(lake, "*.parquet"),
            "rows": {k: merge[k] for k in ("snapshots", "bodies", "enrichments")},
            "fetches_rows": tot[1], "run_manifest_rows": tot[0],
            "raw_partitions_read": merge["raw_files_read"],
        },
        "files": {
            "raw_partition_files": len(raw_files),
            "raw_unsealed_parts_left": sum("_parts" in f for f in raw_files),
            "control_files_after_rollup": len(files(d / "bucket", "control/**/*.parquet")),
            "manifests_rolled_up": merge["manifests_rolled"],
        },
        "mock_totals": {k: week["mock"][k] for k in ("by_adapter", "429_refuser", "429_ip", "503_woo")},
    }


result = {
    "design": "C",
    "scale": "fetch jobs 10x; the sweep's idle waits between waves jump the clock",
    "scenarios": {k: scenario(v) for k, v in DIRS.items()},
}
json.dump(result, open(OUT, "w"), indent=1, default=str)
print(json.dumps({k: v["end_of_week"] | {"all_in_limit": v["all_jobs_finished_inside_limit"]}
                  for k, v in result["scenarios"].items()}, indent=1))
