"""Do dbt's outputs stay the same for JSON-path data, main's ingest against design B's ELT ingest + merge?

usage: dbt_equiv.py OUTDIR
Builds two small local lakes from the recorded storefront JSON in tests/fixtures (3 Shopify stores and 1 WooCommerce
store, two weeks with a price change and a delisting), served by respx. Lake "main" uses origin/main's code
(build/pr66-main, 6bda240): ingest.run, crawl-time classification and rules enrichments. Lake "B" uses B/wt:
per-connector crawl jobs (extract and load only), the weekly merge, and the build's own classification. Each code
dir's own dbt project then runs over its lake (`dbt build` without tests: seeds and models). Every mart and public table is compared, all columns
except timestamps (fetch times differ between runs).
"""
import json, os, subprocess, sys

OUT = os.path.abspath(sys.argv[1])
PY = "/home/user/coffee-data-tracker/.venv/bin/python"
DBT = "/home/user/coffee-data-tracker/.venv/bin/dbt"
CODE = {"main": "/home/user/coffee-data-tracker/build/pr66-main",
        "B": "/home/user/coffee-data-tracker/build/arch-compare/B/wt"}
FIX = "/home/user/coffee-data-tracker/build/arch-compare/B/wt/tests/fixtures"

MAKE = r'''
import copy, json, sys
import httpx, respx
mode, lake_dir, fix = sys.argv[1:4]
from coffeedata import ingest, registry
from coffeedata.lake import Lake
from coffeedata.models import Roaster
stores = {"onyxcoffeelab.com": "shopify_onyxcoffeelab.json", "counterculturecoffee.com": "shopify_counterculturecoffee.json",
          "intelligentsia.com": "shopify_intelligentsia.json", "anaheimcoffee.com": "woocommerce_anaheimcoffee.json"}
data = {h: json.load(open(f"{fix}/{f}")) for h, f in stores.items()}
roasters = [Roaster(roaster_id=h, name=h, host=h, platform="woocommerce" if f.startswith("woo") else "shopify")
            for h, f in stores.items()]
states = [registry.RoasterState(r, "active", None) for r in roasters]
registry.current = lambda lake, seed=None: states
lake = Lake(lake_dir)

def week(d, n):  # week 2: the first product of each store costs more, the last one is gone
    out = copy.deepcopy(d)
    prods = out["products"] if isinstance(out, dict) else out
    if n == 2:
        prods.pop()
        p = prods[0]
        for v in p.get("variants") or []:
            if "price" in v:
                v["price"] = str(float(v["price"] or 0) + 1)
        if "prices" in p:
            p["prices"]["price"] = str(int(p["prices"]["price"]) + 100)
    return out

for n, run_date in ((1, "2026-10-26"), (2, "2026-11-02")):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        for h, d in data.items():
            body = week(d, n)
            if isinstance(body, dict):
                m.get(f"https://{h}/products.json", params={"page": "1"}).mock(return_value=httpx.Response(200, json=body))
                m.get(f"https://{h}/products.json", params={"page": "2"}).mock(return_value=httpx.Response(200, json={"products": []}))
            else:
                m.get(f"https://{h}/wp-json/wc/store/v1/products", params={"page": "1"}).mock(return_value=httpx.Response(200, json=body))
                m.get(f"https://{h}/wp-json/wc/store/v1/products", params={"page": "2"}).mock(return_value=httpx.Response(200, json=[]))
        m.route().mock(return_value=httpx.Response(404))
        if mode == "main":
            print(json.dumps(ingest.run(lake, states, run_date, workers=4, mode="full")["by_adapter"]))
        else:
            from coffeedata import connectors
            for c in ("shopify", "woocommerce"):
                print(json.dumps(connectors.crawl(lake, c, run_date)["by_adapter"]))
            print(json.dumps(connectors.merge(lake, run_date)["written"]))
'''

DUMP = r'''
import json, sys, duckdb
con = duckdb.connect(sys.argv[1], read_only=True)
out = {}
for (t,) in con.execute("select table_name from information_schema.tables where table_schema='main' order by 1").fetchall():
    if not (t.startswith(("dim_", "fct_", "mart_", "public_", "run_completeness", "int_"))):
        continue
    cols = [c for c, ty in con.execute(f"select column_name, data_type from information_schema.columns where table_name='{t}' and table_schema='main' order by ordinal_position").fetchall()
            if "TIMESTAMP" not in ty and c not in ("build_id", "built_at", "code_version", "release_id")]
    rows = con.execute(f"select {', '.join(chr(34) + c + chr(34) for c in cols)} from {t}").fetchall()
    out[t] = {"cols": cols, "rows": sorted(json.dumps(r, default=str) for r in rows)}
json.dump(out, open(sys.argv[2], "w"))
'''

res = {}
for name, code in CODE.items():
    d = f"{OUT}/{name}"
    os.makedirs(d, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=f"{code}/src", UV_NO_SYNC="1", COFFEEDATA_DB=f"{d}/coffee.duckdb")
    for k in [k for k in env if "ANTHROPIC" in k or k.startswith(("HF_", "HUGGING"))]:
        env.pop(k)
    mk = subprocess.run([PY, "-c", MAKE, name, f"{d}/lake", FIX], env=env, cwd=code, capture_output=True, text=True)
    assert mk.returncode == 0, mk.stderr[-3000:]
    vars_ = json.dumps({"lake_roots": [f"{d}/lake"], "config_dir": FIX})
    r = subprocess.run([DBT, "build", "--exclude", "resource_type:unit_test", "resource_type:test", "--project-dir", "dbt", "--profiles-dir", "dbt", "--vars", vars_,
                        "--target-path", f"{d}/target", "--log-path", f"{d}/logs"], env=env, cwd=code, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:]
    subprocess.run([PY, "-c", DUMP, f"{d}/coffee.duckdb", f"{d}/marts.json"], check=True)
    res[name] = {"ingest": mk.stdout.strip().splitlines(), "marts": json.load(open(f"{d}/marts.json"))}

a, b = res["main"]["marts"], res["B"]["marts"]
report = {"tables": {}, "ingest": {k: v["ingest"] for k, v in res.items()}}
for t in sorted(set(a) | set(b)):
    if t not in a or t not in b:
        report["tables"][t] = "only in " + ("B" if t in b else "main")
        continue
    same_cols = a[t]["cols"] == b[t]["cols"]
    same = same_cols and a[t]["rows"] == b[t]["rows"]
    report["tables"][t] = {"rows": len(a[t]["rows"]), "identical": same} | (
        {} if same else {"only_main": sorted(set(a[t]["rows"]) - set(b[t]["rows"]))[:5],
                         "only_B": sorted(set(b[t]["rows"]) - set(a[t]["rows"]))[:5], "cols_same": same_cols})
report["all_identical"] = all(isinstance(v, dict) and v["identical"] for v in report["tables"].values())
json.dump(report, open(f"{OUT}/report.json", "w"), indent=1)
print(json.dumps({t: v if isinstance(v, str) else (v["rows"], v["identical"]) for t, v in report["tables"].items()}))
print("all_identical", report["all_identical"])
