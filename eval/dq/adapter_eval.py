"""#11 per-adapter volume bands, replayed over the recorded runs (eval/dq/README.md).

    python adapter_eval.py cells   <replay-dir> <tag> <data-dir>   # per (set, run, adapter) metrics + label inputs
    python adapter_eval.py predict <data-dir> <rule> <out.json>
    python adapter_eval.py score   <data-dir> <labels-dir> <set> <pred.json>... [--examples N]

<replay-dir> holds coffee-<tag>-<run>.duckdb (a build of the lake cut off at each run), build-<tag>-<run>.log and
that lake, lake-<tag>-<run>/ (its raw fetches carry the HTTP statuses and rate limits).
A cell is one adapter in one run over one set's roasters (the roaster split of common.split_of), so every set has its
own cells from the same runs. Metrics, over the cell's roasters that have a catalog (run_completeness.has_catalog):
  ok_share          catalog_ok share (today's adapter_ok_share; main bands it already)
  ok_attempted      catalog_ok share of the roasters the run tried
  ppf_mean          products per catalog-ok fetch, mean
  ppf_median        the same, median
  same_store        products this run / products in each roaster's previous catalog-ok run, over roasters
                    catalog-ok in both
  priced_share      share of this run's products (catalog-ok roasters) with a priced variant
  same_store_priced priced products this run / priced in the previous catalog-ok run, same roasters
A rule judges a cell against the same metric in the history: the cells of the earlier runs whose build passed
(ignoring latest_run_is_fresh, which a replay fails for its date alone), the band of metrics_out_of_band:
median - max(band_k x 1.4826 x MAD, halfwidth), halfwidth absolute for shares, a fraction of the median otherwise.
It judges only adapters with at least min_adapter_roasters (20) roasters with a catalog in the whole run, and only
with at least `min_builds` history runs. Rule string: '<metric>:<halfwidth>[+<metric>:<halfwidth>...][,min_builds=<n>]';
'none' flags nothing; ok_share at 0.05 is main's rule.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

import duckdb
from common import SETS, fmt, reference, split_of

RUNS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-30", "2026-11-02"]
SHARES = {"ok_share", "ok_attempted", "priced_share", "same_store", "same_store_priced"}
BAND_K = 1.5
MIN_ROASTERS = 20

ROWS = """
with rc as (select * from run_completeness where has_catalog),
rr as (select * from int_roaster_runs),
prev as (
    select r.roaster_id, arg_max(r.products, r.run_date) as products, arg_max(r.run_date, r.run_date) as run_date
    from rr r where r.run_date < ? and r.ok and r.products > 0 group by r.roaster_id
),
snap as (
    select roaster_id, run_date, count(distinct product_id) as products,
           count(distinct product_id) filter (where list_bool_or(list_transform(variants, lambda v: v.price_usd > 0)))
               as priced
    from stg_snapshots group by all
)
select rc.roaster_id, rc.adapter, rc.attempted, rc.ok, rc.catalog_ok, t.products,
       p.products as prev_products, s.priced, s.products as snap_products, ps.priced as prev_priced,
       ps.products as prev_snap_products
from rc
left join rr t on t.roaster_id = rc.roaster_id and t.run_date = rc.run_date
left join prev p on p.roaster_id = rc.roaster_id
left join snap s on s.roaster_id = rc.roaster_id and s.run_date = rc.run_date
left join snap ps on ps.roaster_id = rc.roaster_id and ps.run_date = p.run_date
"""

FETCH = """
select f.roaster_id, f.status, f.error, f.rate_limited, f.http_statuses
from read_parquet(?, hive_partitioning = true, union_by_name = true) f
join run_completeness rc using (roaster_id) where f.run_date = ? and rc.has_catalog
"""


def passed(log: Path) -> bool:
    fails = set(re.findall(r"(?:FAIL|ERROR) \d+ (\w+)", log.read_text()))
    return not (fails - {"latest_run_is_fresh"})


def error_class(e: str | None) -> str:
    if not e:
        return "none"
    e = re.sub(r"https?://\S+", "<url>", e)
    return re.sub(r"\d{3,}", lambda m: m.group(0) if len(m.group(0)) == 3 else "<n>", e)[:90]


def metrics(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["catalog_ok"]]
    both = [r for r in ok if r["prev_products"]]
    priced_both = [r for r in ok if r["prev_priced"] and r["priced"] is not None]
    prods = [r["products"] or 0 for r in ok]
    snap = sum(r["snap_products"] or 0 for r in ok)
    return {
        "roasters": len(rows),
        "attempted": sum(r["attempted"] for r in rows),
        "fetched_ok": sum(r["ok"] for r in rows),
        "catalog_ok": len(ok),
        "ok_share": len(ok) / len(rows) if rows else None,
        "ok_attempted": len(ok) / sum(r["attempted"] for r in rows)
        if any(r["attempted"] for r in rows)
        else None,
        "ppf_mean": statistics.mean(prods) if prods else None,
        "ppf_median": statistics.median(prods) if prods else None,
        "same_store": sum(r["products"] for r in both) / sum(r["prev_products"] for r in both)
        if both
        else None,
        "same_store_n": len(both),
        "priced_share": sum(r["priced"] or 0 for r in ok) / snap if snap else None,
        "same_store_priced": (
            sum(r["priced"] for r in priced_both) / sum(r["prev_priced"] for r in priced_both)
            if priced_both
            else None
        ),
        "prev_products": sum(r["prev_products"] for r in both),
        "this_products": sum(r["products"] for r in both),
        "prev_priced": sum(r["prev_priced"] for r in priced_both),
        "this_priced": sum(r["priced"] for r in priced_both),
    }


def cmd_cells(replay: Path, tag: str, data: Path) -> None:
    cells, inputs = [], []
    for run in RUNS:
        con = duckdb.connect(str(replay / f"coffee-{tag}-{run}.duckdb"), read_only=True)
        cur = con.execute(ROWS, [run])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        fetch = con.execute(
            FETCH, [str(replay / f"lake-{tag}-{run}" / "fetches" / "*" / "*.parquet"), run]
        ).fetchall()
        ok_build = passed(replay / f"build-{tag}-{run}.log")
        full = {}
        for r in rows:
            full[r["adapter"]] = full.get(r["adapter"], 0) + 1
        for s in (*SETS, "all"):
            for adapter in sorted(full):
                sub = [
                    r
                    for r in rows
                    if r["adapter"] == adapter and (s == "all" or split_of(r["roaster_id"]) == s)
                ]
                if not sub:
                    continue
                ids = {r["roaster_id"] for r in sub}
                fs = [f for f in fetch if f[0] in ids]
                m = metrics(sub)
                cid = f"{s}|{run}|{adapter}"
                cells.append(
                    {
                        "id": cid,
                        "split": s,
                        "run": run,
                        "adapter": adapter,
                        "build_passed": ok_build,
                        "adapter_roasters_all": full[adapter],
                        **m,
                    }
                )
                if s != "all":
                    inputs.append({"id": cid, "evidence": evidence(run, adapter, m, fs)})
    # Context: the same stores' ok counts in every run, so a labeler can tell the platform's usual failure rate.
    by = {c["id"]: c for c in cells}
    for x in inputs:
        s, _, adapter = x["id"].split("|")
        runs = [by.get(f"{s}|{r}|{adapter}") for r in RUNS]
        ctx = "; ".join(f"{c['run']}: {c['catalog_ok']}/{c['roasters']}" for c in runs if c)
        x["evidence"] += f"\nThe same subset in each run (succeeded with products / expected): {ctx}"
    data.mkdir(parents=True, exist_ok=True)
    (data / "adapter_cells.json").write_text(json.dumps(cells, indent=1))
    (data / "adapter_inputs.json").write_text(json.dumps(inputs, indent=1))
    print(len(cells), "cells,", len(inputs), "to label")


def evidence(run: str, adapter: str, m: dict, fs: list) -> str:
    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    rate_limited = 0
    for _, status, err, rl, _hs in fs:
        statuses[status] = statuses.get(status, 0) + 1
        if status != "ok":
            errors[error_class(err)] = errors.get(error_class(err), 0) + 1
        rate_limited += rl or 0
    http: dict[int, int] = {}
    for f in fs:
        for h in f[4] or []:
            http[h["status"]] = http.get(h["status"], 0) + h["responses"]
    top = sorted(errors.items(), key=lambda kv: -kv[1])[:8]
    return "\n".join(
        [
            f"Run {run}, platform {adapter} (this subset of stores)",
            f"Stores with a catalog expected: {m['roasters']}; tried: {m['attempted']}; a fetch succeeded: "
            f"{m['fetched_ok']}; succeeded with products: {m['catalog_ok']}",
            f"Fetch attempts by status: {statuses}",
            f"Failed attempts by error: {dict(top)}",
            f"HTTP responses by status (recorded from 2026-10-30 on): {http or 'not recorded'}; rate limited: {rate_limited}",
            f"Stores fetched with products both now and in their previous successful run: {m['same_store_n']}; their "
            f"products then {m['prev_products']}, now {m['this_products']}; priced products then {m['prev_priced']}, "
            f"now {m['this_priced']}",
            f"Products per store that came back: mean {m['ppf_mean'] and round(m['ppf_mean'], 1)}, "
            f"median {m['ppf_median']}",
        ]
    )


def parse(rule: str) -> tuple[list[tuple[str, float]], int]:
    if rule == "none":
        return [], 1
    parts = rule.split(",")
    terms = [(t.split(":")[0], float(t.split(":")[1])) for t in parts[0].split("+")]
    opts = dict(p.split("=") for p in parts[1:])
    return terms, int(opts.get("min_builds", 1))


def judge(cells: list[dict], rule: str) -> dict[str, bool | None]:
    """True: flagged; False: judged in band; None: not judged (abstains: no history, or too few roasters)."""
    terms, min_builds = parse(rule)
    by = {(c["split"], c["run"], c["adapter"]): c for c in cells}
    out: dict[str, bool | None] = {}
    for c in cells:
        if c["adapter_roasters_all"] < MIN_ROASTERS:
            out[c["id"]] = None
            continue
        hist_runs = [
            r for r in RUNS if r < c["run"] and by.get((c["split"], r, c["adapter"]), {}).get("build_passed")
        ]
        if len(hist_runs) < min_builds:
            out[c["id"]] = None
            continue
        flag = False
        for metric, hw in terms:
            hist = [by[(c["split"], r, c["adapter"])][metric] for r in hist_runs]
            hist = [h for h in hist if h is not None]
            if not hist or c[metric] is None:
                continue
            med = statistics.median(hist)
            mad = statistics.median(abs(h - med) for h in hist)
            half = max(BAND_K * 1.4826 * mad, hw if metric in SHARES else hw * abs(med))
            flag = flag or c[metric] < med - half
        out[c["id"]] = flag
    return out


def cmd_predict(data: Path, rule: str, out: str) -> None:
    cells = json.loads((data / "adapter_cells.json").read_text())
    Path(out).write_text(json.dumps({"rule": rule, "pred": judge(cells, rule)}))


def cmd_score(data: Path, labels: Path, which: str, preds: list[str], examples: int) -> None:
    cells = {c["id"]: c for c in json.loads((data / "adapter_cells.json").read_text()) if c["split"] == which}
    ref = reference(labels, "adapter")
    for path in preds:
        pr = json.loads(Path(path).read_text())
        pred = {k: v for k, v in pr["pred"].items() if k in cells}
        judged = [k for k, v in pred.items() if v is not None]
        flagged = [k for k in judged if pred[k]]
        pos = [k for k in judged if ref[k] == "collapse"]
        print(f"[{which}] {pr['rule']}: {len(cells)} cells, judged {len(judged)}, flagged {len(flagged)}")
        print(
            f"  precision {fmt(sum(ref[k] == 'collapse' for k in flagged), len(flagged))}; "
            f"confidently wrong (normal but flagged) {fmt(sum(ref[k] == 'normal' for k in flagged), len(flagged))}"
        )
        print(
            f"  recall of judged collapses {fmt(sum(pred[k] for k in pos), len(pos))}; collapses not judged "
            f"(abstained) {sum(ref[k] == 'collapse' and pred.get(k) is None for k in cells)}"
        )
        if examples and which == "train":
            for k in judged:
                if pred[k] != (ref[k] == "collapse"):
                    print(f"    {ref[k]:8} pred={pred[k]} {k}")


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "cells":
        cmd_cells(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    elif cmd == "predict":
        cmd_predict(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    elif cmd == "score":
        ex = int(sys.argv[sys.argv.index("--examples") + 1]) if "--examples" in sys.argv else 0
        args = [a for a in sys.argv[5:] if a != "--examples" and not a.isdigit()]
        cmd_score(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], args, ex)


if __name__ == "__main__":
    main()
