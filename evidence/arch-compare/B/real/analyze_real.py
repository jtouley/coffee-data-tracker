"""Summarize B's real Shopify crawl: responses log + the job's raw partition. usage: analyze_real.py [A_REFUSED.txt]"""
import glob, json, os, sys
from collections import Counter, defaultdict
from datetime import datetime

import duckdb

B = "/home/user/coffee-data-tracker/build/arch-compare/B"
NOT_SENT = {"HostRateLimited", "RateLimited"}  # the limiter refused before any request left
allrows = [json.loads(l) for l in open(f"{B}/real_responses.jsonl")]
rows = [r for r in allrows if not (r["status"] is None and r.get("error") in NOT_SENT)]
for r in rows:
    r["t"] = datetime.fromisoformat(r["ts"]).timestamp()
rows.sort(key=lambda r: r["t"])
out = json.load(open(f"{B}/real/crawl_out.json"))
retry = json.load(open(f"{B}/real/retry_out.json")) if os.path.exists(f"{B}/real/retry_out.json") else {}
st = Counter(r["status"] for r in rows)
t0, t1 = rows[0]["t"], rows[-1]["t"]
h429 = Counter(r["host"] for r in rows if r["status"] == 429)
by_host = defaultdict(list)
for r in rows:
    by_host[r["host"]].append(r)
first_cat_429 = sum(1 for h, L in by_host.items()
                    if any(x["path"] == "/products.json" for x in L)
                    and next(x for x in L if x["path"] == "/products.json")["status"] == 429)
nxt = Counter()
for h, L in by_host.items():
    for a, b in zip(L, L[1:]):
        if a["status"] == 429:
            nxt[b["status"]] += 1
# 429s per 10-minute bucket, and how many distinct hosts each bucket's 429s came from
buckets = defaultdict(lambda: [0, 0, set()])
for r in rows:
    b = int((r["t"] - t0) // 600)
    buckets[b][0] += 1
    if r["status"] == 429:
        buckets[b][1] += 1
        buckets[b][2].add(r["host"])
part = glob.glob(f"{B}/real/lake/raw/connector=shopify/run_date=*/fetches.parquet")
con = duckdb.connect()
f = con.execute(f"""select roaster_id, bool_or(status='ok') ok, bool_or(status='ok' and products>0) cat,
    arg_max(error, fetched_at) last_error from read_parquet({part}) group by 1""").fetchall()
ok = sum(r[1] for r in f)
refused = sorted(r[0] for r in f if not r[1] and (r[3] or "").startswith("refused"))
failed = [r for r in f if not r[1]]
res = {
    "wall_min": round(out["wall_s"] / 60, 1), "finished_inside_240": out["wall_s"] < 240 * 60 and not out.get("skipped_deadline"),
    "requests": len(rows), "limiter_refusals_not_sent": len(allrows) - len(rows), "status": {str(k): v for k, v in st.most_common()},
    "429": st[429], "430": st[430], "hosts_with_429": len(h429), "hosts": len(by_host),
    "hosts_whose_first_products_json_was_429": first_cat_429,
    "next_status_after_429_same_host": {str(k): v for k, v in nxt.items()},
    "retry_after": dict(Counter(r["retry_after"] for r in rows if r["status"] == 429)),
    "top_429_hosts": h429.most_common(10),
    "per_10min": [(b, v[0], v[1], len(v[2])) for b, v in sorted(buckets.items())],
    "roasters": len(f), "todo": out.get("todo"), "ok": ok, "catalog_ok": sum(bool(r[2]) for r in f),
    "failed": len(failed), "refused": len(refused),
    "failure_kinds": Counter((r[3] or "")[:40].split(":")[0] for r in failed).most_common(8),
    "limiter": out.get("shopify_limiter"), "deferred": out.get("deferred"),
    "retry_job": {k: retry.get(k) for k in ("parked_in", "wall_s", "deferred", "shopify_limiter", "by_adapter")},
    "by_phase": {ph: dict(Counter(str(r["status"]) for r in rows if r.get("phase") == ph)) for ph in ("crawl", "crawl-retry")},
}
okset = {r[0] for r in f if r[1]}
for name, path in (("page2_11", f"{B}/real/retest_page2.txt"), ("A_refused_22", f"{B}/real/A_refused.txt")):
    ids = {l.strip() for l in open(path) if l.strip()}
    res[name] = {"stores": len(ids), "ok": len(ids & okset), "refused_again": len(ids & set(refused)),
                 "ok_ids": sorted(ids & okset)}
if len(sys.argv) > 1:
    a = {l.strip() for l in open(sys.argv[1]) if l.strip()}
    res["A_refused"] = len(a)
    res["A_refused_refused_again_in_B"] = len(a & set(refused))
    res["A_refused_ok_in_B"] = len(a & {r[0] for r in f if r[1]})
json.dump(res, open(f"{B}/real/summary.json", "w"), indent=1, default=str)
open(f"{B}/real/refused.txt", "w").write("\n".join(refused) + "\n")
print(json.dumps(res, default=str, indent=1))
