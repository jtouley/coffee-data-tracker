"""Metrics of the real Shopify crawl: real_responses.jsonl + the local lake's fetches -> real/metrics.json"""
import json, math, random, collections
from datetime import datetime
import duckdb

A = "/home/user/coffee-data-tracker/build/arch-compare/A"
S = json.load(open(f"{A}/real2/summary.json"))
ALL = [json.loads(l) for l in open(f"{A}/real2_responses.jsonl")]
# The limiter's own refusals (HostRateLimited/RateLimited, raised before sending) are logged but never left the box.
LOCAL = ("HostRateLimited", "RateLimited")
R = [r for r in ALL if r.get("error") not in LOCAL]
for r in R:
    r["t"] = datetime.fromisoformat(r["ts"]).timestamp()
R.sort(key=lambda r: r["t"])


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]


st = collections.Counter(r["status"] for r in R)
t429 = [r for r in R if r["status"] == 429]
hosts = {r["host"] for r in R}
h429 = collections.Counter(r["host"] for r in t429)
# first catalog request per host: was it a 429?
first = {}
for r in R:
    if r["path"] == "/products.json" and r["status"] is not None and r["host"] not in first:
        first[r["host"]] = r["status"]
first429 = sum(s == 429 for s in first.values())
# after a 429 on host h, is the next request to h another 429?
nxt = collections.Counter()
last = {}
for r in R:
    if r["status"] is None:
        continue
    if last.get(r["host"]) == 429:
        nxt[r["status"] == 429] += 1
    last[r["host"]] = r["status"]
# cross-host contagion: P(429 | another host's 429 in previous 30 s) vs otherwise (catalog requests)
cat = [r for r in R if r["path"] == "/products.json" and r["status"] is not None]
recent, a, b = [], [0, 0], [0, 0]
for r in cat:
    recent = [x for x in recent if r["t"] - x[0] < 30]
    other = any(h != r["host"] for _, h in recent)
    (a if other else b)[0] += r["status"] == 429
    (a if other else b)[1] += 1
    if r["status"] == 429:
        recent.append((r["t"], r["host"]))
# 429s per minute vs requests per minute
t0 = R[0]["t"]
per_min = collections.defaultdict(lambda: [0, 0])
for r in R:
    m = int((r["t"] - t0) // 60)
    per_min[m][0] += 1
    per_min[m][1] += r["status"] == 429
mins = sorted(per_min)
con = duckdb.connect()
fx = f"(select * from read_parquet('{A}/real2/lake/fetches/*/*.parquet', union_by_name=true) where run_date = '2026-11-01')"
rows = con.execute(f"""with r as (select roaster_id, bool_or(status='ok') ok, bool_or(status='ok' and products>0) cat,
     arg_max(error, fetched_at) last_err from {fx} where roaster_id is not null group by 1)
  select count(*), sum(ok::int), sum(cat::int), sum(coalesce(last_err like 'refused:%', false)::int),
         sum((not ok and not coalesce(last_err like 'refused:%', false))::int) from r""").fetchone()
errs = con.execute(f"""with r as (select roaster_id, bool_or(status='ok') ok, arg_max(error, fetched_at) e from {fx}
   where roaster_id is not null group by 1) select regexp_extract(e, '^(refused|[A-Za-z]+)', 1) k, count(*) from r where not ok group by 1 order by 2 desc""").fetchall()
n, ok, catok, refused, failed = rows
m = {
    "started": S["started"], "finished": S["finished"], "wall_min": S["wall_min"], "main_pass_min": S["main_wall_min"],
    "inside_240_min": S["wall_min"] <= 240, "stopped_at_deadline": S["main"].get("stopped_at_deadline"),
    "roasters": S["roasters"], "distinct_hosts_contacted": len(hosts),
    "requests": len(R), "limiter_refusals_not_sent": len(ALL) - len(R), "statuses": {str(k): v for k, v in st.most_common()},
    "429s": st.get(429, 0), "430s": st.get(430, 0), "network_errors": st.get(None, 0),
    "stores": {"attempted": n, "ok": ok, "ok_with_products": catok, "refused": refused, "failed_other": failed,
               "ok_share": round(ok / n, 4), "ok_wilson95_pct": wilson(ok, n), "failure_kinds": dict(errs)},
    "limiter": S["main"]["shopify_limiter"], "deferred": S["main"]["deferred"],
    "crawl_retry": {k: (S.get("crawl_retry") or {}).get(k) for k in ("roasters", "shopify_limiter", "deferred")}
    | {"todo": S.get("crawl_retry_todo"), "wall_min": S.get("crawl_retry_wall_min")},
    "job1_wall_min": S.get("job1_wall_min"),
    "retry_pass": {k: S.get("retry", {}).get(k) for k in ("roasters", "fetched", "shopify_limiter", "deferred")} | {"todo": S.get("retry_todo")},
    "pattern_429": {
        "hosts_with_429": len(h429), "hosts_contacted": len(hosts),
        "share_hosts_with_429": round(len(h429) / len(hosts), 4), "share_hosts_wilson95_pct": wilson(len(h429), len(hosts)),
        "top_hosts_share_of_429s": round(sum(c for _, c in h429.most_common(max(1, len(h429) // 5))) / max(1, len(t429)), 3),
        "first_catalog_request_429": [first429, len(first)],
        "next_request_same_host_after_429_is_429": [nxt[True], nxt[True] + nxt[False]],
        "p429_after_other_hosts_429_in_30s": [a[0], a[1]], "p429_otherwise": [b[0], b[1]],
        "retry_after_values": dict(collections.Counter(r["retry_after"] for r in t429).most_common(5)),
        "max_429s_in_one_minute": max(per_min[k][1] for k in mins), "minutes": len(mins),
        "median_req_per_min": sorted(per_min[k][0] for k in mins)[len(mins) // 2],
    },
}
json.dump(m, open(f"{A}/real2/metrics.json", "w"), indent=1, default=str)
print(json.dumps(m, indent=1, default=str))
