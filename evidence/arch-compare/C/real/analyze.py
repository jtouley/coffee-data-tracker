"""Summarize Design C's real condensed week: C/real/jobs/*.json, C/real_responses.jsonl, the control manifests.

usage: analyze.py [A_REFUSED.txt B_REFUSED.txt]   (each: one host or roaster_id per line; optional)
Prints markdown tables and writes C/real/summary.json.
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

C = Path("/home/user/coffee-data-tracker/build/arch-compare/C")
R = C / "real"
jobs = {p.stem: json.loads(p.read_text()) for p in sorted((R / "jobs").glob("*.json"))}
resp = [json.loads(line) for line in open(C / "real_responses.jsonl")]
ctrl = [r for p in sorted((R / "bucket/control").rglob("*.parquet")) for r in pq.read_table(p).to_pylist()]
ctrl.sort(key=lambda r: r["ended_at"])


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return f"{100 * k / n:.1f}% [{100 * (c - h):.1f}, {100 * (c + h):.1f}]"


by_job = defaultdict(list)
for r in resp:
    by_job[r["job"]].append(r)
out = ["| job | stores | wall | requests | 429s | hosts with 429 | ok | refused (attempt) | parked | error |",
       "|---|---|---|---|---|---|---|---|---|---|"]
summary = {"jobs": {}}
for tag in [f"shard{k}" for k in range(7)] + ["sweep"]:
    j, rs = jobs.get(tag), by_job.get(tag, [])
    if j is None:
        out.append(f"| {tag} | not run | | | | | | | | |")
        continue
    rows = [r for r in ctrl if (r["job_id"].startswith(f"shopify-") and r["shard"] == tag.removeprefix("shard"))
            or (tag == "sweep" and r["job"] == "sweep")]
    oc = Counter(r["outcome"] for r in rows)
    sent = [r for r in rs if r["status"] is not None]  # status None: the limiter refused it, never sent
    n429 = sum(r["status"] == 429 for r in rs)
    hosts429 = len({r["host"] for r in rs if r["status"] == 429})
    stores = j.get("stores", j.get("candidates"))
    out.append(f"| {tag} | {stores} | {j['wall_s'] / 60:.1f} min | {len(sent)} | {n429} | {hosts429} | {oc['ok']} | "
               f"{oc['refused_attempt'] + oc['refused']} | {oc['parked']} | {oc['error']} |")
    summary["jobs"][tag] = {"stores": stores, "wall_min": round(j["wall_s"] / 60, 1), "requests": len(sent), "limiter_refusals_not_sent": len(rs) - len(sent),
                            "429": n429, "hosts_429": hosts429, "outcomes": dict(oc),
                            "within_240": j["wall_s"] <= 240 * 60}
print("\n".join(out))
# Final state per store: the last API outcome, ok anywhere wins
final: dict[str, str] = {}
for r in ctrl:
    if r["outcome"] == "html_stub":
        continue
    if final.get(r["roaster_id"]) != "ok":
        final[r["roaster_id"]] = r["outcome"]
fc = Counter("refused" if v in ("refused", "refused_attempt") else v for v in final.values())
n = len(final)
print(f"\nShopify stores: {n}; ok {fc['ok']} = {wilson(fc['ok'], n)}; refused {fc['refused']}; "
      f"still parked {fc['parked']}; other errors {fc['error']}")
routed = [r["roaster_id"] for r in ctrl if r["outcome"] == "html_stub"]
sw = jobs.get("sweep", {})
recovered = sum(1 for r in ctrl if r["job"] == "sweep" and r["outcome"] == "ok")
print(f"Sweep: {sw.get('candidates')} parked candidates, recovered {recovered}; waves {sw.get('waves')}")
print(f"HTML stub: routed {len(set(routed))}")
summary |= {"final": dict(fc), "stores": n, "sweep_recovered": recovered, "routed": len(set(routed))}
# 429 pattern
shop = [r for r in resp if r["status"] is not None]
ra = Counter(r["retry_after"] for r in shop if r["status"] == 429)
by_host = defaultdict(list)
for r in shop:
    by_host[r["host"]].append(r)
h429 = {h for h, rs in by_host.items() if any(r["status"] == 429 for r in rs)}
cat = lambda h: [r for r in by_host[h] if r["path"] == "/products.json"]  # noqa: E731
first429 = sum(1 for h in h429 if cat(h) and cat(h)[0]["status"] == 429)
always = sum(1 for h in h429 if all(r["status"] == 429 for r in cat(h)))
robots429 = sum(1 for r in shop if r["path"] == "/robots.txt" and r["status"] == 429)
page2 = sum(1 for r in shop if r["status"] == 429 and str(r.get("page")) not in ("1", "None"))
recovered_hosts = sum(1 for h in h429 if any(r["status"] == 200 and r["path"] != "/robots.txt" for r in by_host[h]))
t = lambda r: datetime.fromisoformat(r["ts"])  # noqa: E731
mins = Counter(t(r).strftime("%H:%M") for r in shop if r["status"] == 429)
statuses = Counter(r["status"] for r in shop)
print(f"\n429 pattern: {sum(statuses[s] for s in (429,))} 429s of {len(shop)} responses, on {len(h429)} hosts; "
      f"first response a 429 on {first429}; only ever 429 on {always}; got a catalog 200 later on {recovered_hosts}; "
      f"Retry-After {dict(ra.most_common(5))}; statuses {dict(statuses.most_common(8))}; "
      f"busiest minute {mins.most_common(1)}; 430s {statuses[430]}; robots.txt 429s {robots429}; 429s on page>=2 {page2}; "
      f"hosts contacted {len(by_host)}")
summary["pattern"] = {"responses": len(shop), "429": statuses[429], "hosts_429": len(h429), "first_is_429": first429,
                      "only_429": always, "robots_429": robots429, "page2plus_429": page2, "hosts": len(by_host), "later_200": recovered_hosts, "retry_after": dict(ra), "statuses": dict(statuses),
                      "430": statuses[430]}
refused_ids = sorted(k for k, v in final.items() if v in ("refused", "refused_attempt"))
(R / "refused.txt").write_text("\n".join(refused_ids) + "\n")
for name, path in zip(("A", "B"), sys.argv[1:3], strict=False):
    other = {line.strip() for line in open(path) if line.strip()}
    mine = set(refused_ids)
    print(f"Overlap with {name}: {len(mine & other)} of ours {len(mine)}, theirs {len(other)}")
    summary[f"overlap_{name}"] = {"both": len(mine & other), "ours": len(mine), "theirs": len(other)}
json.dump(summary, open(R / "summary.json", "w"), indent=1, default=str)
# A's 11 stores that failed on a page-2 429 (pagination_11.txt), under the resumable-pagination patch
eleven = [line.strip() for line in open(R / "pagination_11.txt") if line.strip()]
res11 = {rid: final.get(rid, "not fetched") for rid in eleven}
print(f"\nPagination patch, A's 11 page-2 failures: {sum(v == 'ok' for v in res11.values())} of {len(eleven)} ok: {res11}")
summary["pagination_11"] = res11
json.dump(summary, open(R / "summary.json", "w"), indent=1, default=str)
# A's 22 refused stores (B/real/A_refused.txt): what C got for each
a22 = [line.strip() for line in open(C.parent / "B/real/A_refused.txt") if line.strip()]
res22 = Counter("refused" if final.get(r) in ("refused", "refused_attempt") else final.get(r, "not fetched") for r in a22)
print(f"A's {len(a22)} refused stores in C: {dict(res22)}")
summary["A_refused_22"] = {"outcomes": dict(res22), "per_store": {r: final.get(r, "not fetched") for r in a22}}
json.dump(summary, open(R / "summary.json", "w"), indent=1, default=str)
