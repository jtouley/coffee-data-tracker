"""Per-store results of the targeted retest -> B/real/retest_summary.json (and a markdown table on stdout)."""
import json, os, sys
from collections import defaultdict
from datetime import datetime

import duckdb

B = "/home/user/coffee-data-tracker/build/arch-compare/B"
WT = f"{B}/wt"
os.chdir(WT)
sys.path.insert(0, f"{WT}/src")
os.environ["COFFEEDATA_LAKE"] = "/tmp/b_reg_lake"
from coffeedata import registry  # noqa: E402
from coffeedata.lake import Lake  # noqa: E402

want = [l.strip() for l in open(f"{B}/real/retest_list.txt") if l.strip()]
page2 = {l.strip() for l in open(f"{B}/real/retest_page2.txt") if l.strip()}
reg = {s.roaster.roaster_id: s.roaster for s in registry.current(Lake())}
norm = lambda h: (h or "").lower().removeprefix("www.")  # noqa: E731
host_of = {norm(reg[r].host): r for r in want}
NOT_SENT = {"HostRateLimited", "RateLimited"}  # the limiter refused before any request left
allrows = [json.loads(l) for l in open(f"{B}/retest_responses.jsonl")]
rows = [r for r in allrows if not (r["status"] is None and r.get("error") in NOT_SENT)]
for r in rows:
    r["t"] = datetime.fromisoformat(r["ts"]).timestamp()
rows.sort(key=lambda r: r["t"])
by = defaultdict(list)
for r in rows:
    rid = host_of.get(norm(r["host"]))
    if rid is None:  # a redirect target: attribute to the store whose host redirected there most recently
        rid = next((x["rid"] for x in reversed(rows[: rows.index(r)]) if x.get("rid") and x["path"] == r["path"]), None)
    r["rid"] = rid
    if rid:
        by[rid].append(r)
con = duckdb.connect()
lake = f"{B}/real/retest_lake"
f = {r[0]: r[1:] for r in con.execute(f"""select roaster_id, bool_or(status='ok'), max(pages) filter (where status='ok'),
    max(coffees) filter (where status='ok'), max(products) filter (where status='ok'), arg_max(error, fetched_at), count(*)
    from read_parquet('{lake}/fetches/run_date=2*/*.parquet') group by 1""").fetchall()}
snap = defaultdict(dict)
for rid, pid, cof in con.execute(f"select roaster_id, product_id, is_coffee from read_parquet('{lake}/snapshots/run_date=2*/*.parquet')").fetchall():
    snap[rid][pid] = cof
out = []
for rid in want:
    L = by.get(rid, [])
    n429 = sum(r["status"] == 429 for r in L)
    waited = sum(b["t"] - a["t"] for a, b in zip(L, L[1:]) if a["status"] == 429)
    p1 = next((r for r in L if r["path"] == "/products.json" and r.get("ids") is not None), None)
    ok, pages, coffees, products, err, attempts = f.get(rid, (False, None, None, None, "no fetch row", 0))
    p1_coffees = sum(1 for i in (p1 or {}).get("ids", []) if snap[rid].get(i)) if p1 and ok else None
    out.append({"roaster_id": rid, "group": "page2" if rid in page2 else "refused", "ok": bool(ok), "pages": pages,
                "products": products, "coffees": coffees, "page1_products": p1.get("products") if p1 else None,
                "page1_coffees": p1_coffees, "requests": len(L), "pages_200": sum(r["path"] == "/products.json" and r["status"] == 200 for r in L),
                "first_products_json_status": next((r["status"] for r in L if r["path"] == "/products.json"), None),
                "http_429": n429, "waited_after_429_s": round(waited),
                "attempts": attempts, "error": None if ok else (err or "")[:80]})
tot = {g: {"stores": sum(o["group"] == g for o in out), "ok": sum(o["ok"] for o in out if o["group"] == g)}
       for g in ("page2", "refused")}
res = {"run": json.load(open(f"{B}/real/retest_out.json")), "totals": tot, "requests": len(rows),
       "429": sum(r["status"] == 429 for r in rows), "430": sum(r["status"] == 430 for r in rows),
       "wall_min": round((rows[-1]["t"] - rows[0]["t"]) / 60, 1), "stores": out}
json.dump(res, open(f"{B}/real/retest_summary.json", "w"), indent=1, default=str)
print("| store | group | outcome | product pages served (200) | 429s | waited after 429s, s | coffees (page-1 coffees) |\n|---|---|---|---|---|---|---|")
for o in out:
    oc = "ok" if o["ok"] else "failed: " + ("refused (429 on every page-1 try)" if (o["error"] or "").startswith("refused") else (o["error"] or "")[:60])
    print(f"| {o['roaster_id']} | {o['group']} | {oc} | {o['pages_200']} | {o['http_429']} | {o['waited_after_429_s']} | "
          f"{o['coffees'] if o['ok'] else '-'} ({o['page1_coffees'] if o['page1_coffees'] is not None else '-'}) |")
print(json.dumps({k: res[k] for k in ("totals", "requests", "429", "430", "wall_min")}))
