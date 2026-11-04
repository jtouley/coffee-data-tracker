"""#7 price per 100 g outliers within origin and process (eval/dq/README.md).

    python price_eval.py items   <coffee.duckdb> <data-dir>
    python price_eval.py predict <coffee.duckdb> <data-dir> <rule> <out.json>
    python price_eval.py score   <data-dir> <labels-dir> <set> <pred.json>... [--examples N]

Population: listed, non-wholesale coffees with a price_per_100g (int_current_catalog). A rule computes, over the
whole population, the median and MAD of ln(price_per_100g) in the coffee's (origin_country, process) group when that
group has at least `min_group` coffees, else its origin_country group (any process) when that has, else all coffees;
robust z = (ln p - median) / (1.4826 MAD). Rule string: 'lo=<k>,hi=<k>,min_group=<n>' (see parse).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import duckdb
from common import SETS, fmt, reference, split_of

STRATA = [(-1e9, -4), (-4, -3), (-3, -2), (-2, 2), (2, 3), (3, 4), (4, 6), (6, 1e9)]
PER_STRATUM = 15
REF_RULE = {"min_group": 30}


def z_scores(con: duckdb.DuckDBPyConnection, min_group: int) -> dict[str, float]:
    q = f"""
    with p as (
        select coffee_id, origin_country as o, process as pr, ln(price_per_100g) as lp
        from int_current_catalog where not is_wholesale and price_per_100g is not null
    ),
    g2 as (select o, pr, count(*) as n, median(lp) as m from p where o is not null and pr is not null group by all),
    g2m as (select g.o, g.pr, g.n, g.m, median(abs(p.lp - g.m)) as mad from g2 g join p using (o, pr) group by all),
    g1 as (select o, count(*) as n, median(lp) as m from p where o is not null group by all),
    g1m as (select g.o, g.n, g.m, median(abs(p.lp - g.m)) as mad from g1 g join p using (o) group by all),
    g0 as (select median(lp) as m from p),
    g0m as (select g0.m, median(abs(p.lp - g0.m)) as mad from p, g0 group by g0.m)
    select p.coffee_id,
           case when g2m.n >= {min_group} then (p.lp - g2m.m) / nullif(1.4826 * g2m.mad, 0)
                when g1m.n >= {min_group} then (p.lp - g1m.m) / nullif(1.4826 * g1m.mad, 0)
                else (p.lp - g0m.m) / nullif(1.4826 * g0m.mad, 0) end as z
    from p left join g2m using (o, pr) left join g1m using (o) cross join g0m
    """
    return {k: (z if z is not None else 0.0) for k, z in con.execute(q).fetchall()}


def stratum(z: float) -> int:
    return next(i for i, (lo, hi) in enumerate(STRATA) if lo <= z < hi)


def cmd_items(db: str, data: Path) -> None:
    con = duckdb.connect(db, read_only=True)
    z = z_scores(con, REF_RULE["min_group"])
    roaster = dict(con.execute("select coffee_id, roaster_id from int_current_catalog").fetchall())
    pop: dict[tuple[str, int], list[str]] = {}
    for cid, zz in z.items():
        pop.setdefault((split_of(roaster[cid]), stratum(zz)), []).append(cid)
    rng = random.Random(20261104)
    items, inputs, sizes = [], [], {}
    for (s, st), ids in sorted(pop.items()):
        sizes[f"{s}:{st}"] = len(ids)
        for cid in rng.sample(sorted(ids), min(PER_STRATUM, len(ids))):
            items.append({"id": cid, "split": s, "stratum": st, "roaster_id": roaster[cid]})
            inputs.append({"id": cid, "evidence": evidence(con, cid)})
    data.mkdir(parents=True, exist_ok=True)
    (data / "price_items.json").write_text(json.dumps(items, indent=1))
    (data / "price_inputs.json").write_text(json.dumps(inputs, indent=1))
    (data / "price_strata.json").write_text(json.dumps(sizes, indent=1))
    for s in SETS:
        print(
            s,
            sum(i["split"] == s for i in items),
            "items of",
            sum(v for k, v in sizes.items() if k.startswith(s)),
        )


def evidence(con: duckdb.DuckDBPyConnection, cid: str) -> str:
    c = con.execute(
        """select c.roaster_id, r.roaster_name, r.state, c.title, c.origin_country, c.process, c.roast_level,
                  c.is_blend, c.is_decaf, c.price_per_100g
           from int_current_catalog c left join dim_roaster r using (roaster_id) where coffee_id = ?""",
        [cid],
    ).fetchone()
    variants = con.execute(
        """select v.title, v.price_usd, v.grams, v.available from (
               select variants from stg_snapshots where coffee_id = ? order by run_date desc limit 1
           ), unnest(variants) as t(v) limit 15""",
        [cid],
    ).fetchall()
    lines = [
        f"Store: host {c[0]}; name {c[1]!r}; state {c[2]}",
        f"Title: {c[3]}",
        f"Origin: {c[4]}; process: {c[5]}; roast: {c[6]}; blend: {c[7]}; decaf: {c[8]}",
        "Variants (title | price as read, USD | grams as read | available):",
    ]
    lines += [f"  - {t} | {p} | {g} | {a}" for t, p, g, a in variants]
    lines.append(f"price_per_100g: {c[9]}")
    return "\n".join(lines)


def parse(rule: str) -> dict:
    """'lo=<k>,hi=<k>,min_group=<n>': flag z <= -lo or z >= hi; lo=inf turns the low side off. 'none' (main: no
    rule; price_per_100g_in_range only re-checks dim_coffee's filter) flags nothing."""
    if rule == "none":
        rule = "lo=inf,hi=inf,min_group=30"
    d = dict(t.split("=") for t in rule.split(","))
    return {"lo": float(d["lo"]), "hi": float(d["hi"]), "min_group": int(d["min_group"])}


def flagged(z: float, r: dict) -> bool:
    return z >= r["hi"] or z <= -r["lo"]


def cmd_predict(db: str, data: Path, rule: str, out: str) -> None:
    r = parse(rule)
    z = z_scores(duckdb.connect(db, read_only=True), r["min_group"])
    items = json.loads((data / "price_items.json").read_text())
    flag = {i["id"]: flagged(z[i["id"]], r) for i in items}
    total = {s: 0 for s in SETS}
    # The population count flagged per set, for the stratum weights' sanity check.
    con = duckdb.connect(db, read_only=True)
    roaster = dict(con.execute("select coffee_id, roaster_id from int_current_catalog").fetchall())
    for cid, zz in z.items():
        if flagged(zz, r):
            total[split_of(roaster[cid])] += 1
    Path(out).write_text(json.dumps({"rule": rule, "pred": flag, "flagged_population": total}))


def cmd_score(data: Path, labels: Path, which: str, preds: list[str], examples: int) -> None:
    items = [i for i in json.loads((data / "price_items.json").read_text()) if i["split"] == which]
    sizes = json.loads((data / "price_strata.json").read_text())
    ref = reference(labels, "price")
    per = {}
    for i in items:
        per.setdefault(i["stratum"], []).append(i)
    w = {st: sizes[f"{which}:{st}"] / len(v) for st, v in per.items()}
    wrong_total = sum(w[i["stratum"]] for i in items if ref[i["id"]] == "wrong")
    for path in preds:
        pr = json.loads(Path(path).read_text())
        pred = pr["pred"]
        flagged = [i for i in items if pred[i["id"]]]
        tp = [i for i in flagged if ref[i["id"]] == "wrong"]
        fp = [i for i in flagged if ref[i["id"]] == "right"]
        wf = sum(w[i["stratum"]] for i in flagged)
        wtp = sum(w[i["stratum"]] for i in tp)
        wfp = sum(w[i["stratum"]] for i in fp)
        caught = sum(w[i["stratum"]] for i in tp)
        print(f"[{which}] {pr['rule']}: flags {pr['flagged_population'][which]} coffees in this set")
        print(
            f"  labelled sample: precision {fmt(len(tp), len(flagged))}; "
            f"confidently wrong (right but flagged) {fmt(len(fp), len(flagged))}"
        )
        if wf:
            print(
                f"  stratum-weighted: precision {wtp / wf:.3f}, false-flag share {wfp / wf:.3f}, "
                f"recall of wrong prices {caught / wrong_total if wrong_total else float('nan'):.3f} "
                f"(est. {wrong_total:.0f} wrong in the set)"
            )
        if examples and which == "train":
            for i in [i for i in items if pred[i["id"]] != (ref[i["id"]] == "wrong")][:examples]:
                print(f"    {ref[i['id']]:7} pred={pred[i['id']]} stratum={i['stratum']} {i['id']}")


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "items":
        cmd_items(sys.argv[2], Path(sys.argv[3]))
    elif cmd == "predict":
        cmd_predict(sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5])
    elif cmd == "score":
        ex = int(sys.argv[sys.argv.index("--examples") + 1]) if "--examples" in sys.argv else 0
        args = [a for a in sys.argv[5:] if a != "--examples" and not a.isdigit()]
        cmd_score(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], args, ex)


if __name__ == "__main__":
    main()
