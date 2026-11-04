"""#12 near-duplicate roasters: one roaster's catalog listed under two roaster_ids (eval/dq/README.md).

    python dupes_eval.py items   <coffee.duckdb> <data-dir>        # candidate pairs, strata, split, label inputs
    python dupes_eval.py predict <data-dir> <rule> <out.json>      # a candidate rule's verdict per item
    python dupes_eval.py score   <data-dir> <labels-dir> <set> <pred.json>... [--examples N]

A pair's features come from the listed, non-wholesale coffees (int_current_catalog), titles normalized as the dbt
rule does: lower case, runs of anything but a-z0-9 to one space, trimmed.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

import duckdb

from common import SETS, fmt, reference, split_of

FEATURES = """
with t as (
    select distinct roaster_id, trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) as nt
    from int_current_catalog where not is_wholesale and title is not null
),
t2 as (select * from t where nt <> ''),
df as (select nt, count(*) as d from t2 group by nt),
n as (select roaster_id, count(*) as n from t2 group by roaster_id),
pairs as (
    select a.roaster_id as a, b.roaster_id as b, count(*) as shared, count(*) filter (where df.d <= 3) as rare
    from t2 a join t2 b on a.nt = b.nt and a.roaster_id < b.roaster_id join df on df.nt = a.nt
    group by all
)
select p.a, p.b, p.shared, p.rare, na.n as na, nb.n as nb,
       p.shared / least(na.n, nb.n) as cont, p.rare / least(na.n, nb.n) as rcont,
       least(na.n, nb.n) / greatest(na.n, nb.n) as size_ratio
from pairs p join n na on na.roaster_id = p.a join n nb on nb.roaster_id = p.b
"""


def stem(host: str) -> str:
    host = re.sub(r"^www\.", "", host.lower()).split(".myshopify.com")[0]
    base = host.split(".")[0]
    for w in ("coffeeroasters", "coffeeroasting", "roasters", "roastery", "roasting", "coffeeco", "coffee", "cafe"):
        base = base.replace(w, "")
    return re.sub(r"[^a-z0-9]", "", base)


def name_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("coffee", "").replace("roasters", ""))


def similar(a: str, b: str, na: str | None, nb: str | None) -> bool:
    sa, sb = stem(a), stem(b)
    by_host = len(sa) >= 4 and len(sb) >= 4 and (sa == sb or sa.startswith(sb) or sb.startswith(sa))
    ka, kb = name_key(na), name_key(nb)
    return by_host or (len(ka) >= 4 and ka == kb)


def cmd_items(db: str, data: Path) -> None:
    con = duckdb.connect(db, read_only=True)
    feats = con.execute(FEATURES).fetchall()
    cols = ["a", "b", "shared", "rare", "na", "nb", "cont", "rcont", "size_ratio"]
    pairs = [dict(zip(cols, r)) for r in feats]
    names = dict(con.execute("select roaster_id, roaster_name from dim_roaster").fetchall())
    s1, s2, rest = [], [], []
    for p in pairs:
        if p["rare"] >= 2 and p["rcont"] >= 0.1:
            s1.append(p)
        elif similar(p["a"], p["b"], names.get(p["a"]), names.get(p["b"])):
            s2.append(p)
        elif p["shared"] >= 2:
            rest.append(p)
    rng = random.Random(20261104)
    s3 = rng.sample(sorted(rest, key=lambda p: (p["a"], p["b"])), 60)
    strata = {"S1": (s1, len(s1)), "S2": (s2, len(s2)), "S3": (s3, len(rest))}
    # Split: group pairs into clusters (connected components of the labelled pairs), key = the cluster's first id.
    chosen = [(name, p) for name, (ps, _) in strata.items() for p in ps]
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, p in chosen:
        ra, rb = find(p["a"]), find(p["b"])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    items, inputs = [], []
    for name, p in chosen:
        pid = f"{p['a']}|{p['b']}"
        items.append({"id": pid, "stratum": name, "split": split_of(find(p["a"])), **p})
        inputs.append({"id": pid, "evidence": evidence(con, p, names)})
    data.mkdir(parents=True, exist_ok=True)
    (data / "dupes_items.json").write_text(json.dumps(items, indent=1, default=float))
    (data / "dupes_inputs.json").write_text(json.dumps(inputs, indent=1))
    (data / "dupes_strata.json").write_text(json.dumps({k: n for k, (_, n) in strata.items()}))
    for s in SETS:
        print(s, sum(i["split"] == s for i in items), "items")
    print({k: (len(v), n) for k, (v, n) in strata.items()})


def evidence(con: duckdb.DuckDBPyConnection, p: dict, names: dict) -> str:
    def store(r: str) -> str:
        row = con.execute(
            "select roaster_name, city, state, platform from dim_roaster where roaster_id = ?", [r]
        ).fetchone()
        return f"host {r}; name {row[0]!r}; town {row[1]}, {row[2]}; platform {row[3]}"

    q = """
    with c as (
        select roaster_id, trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) as nt, any_value(title) as title,
               min(min_price_usd) as price, any_value(bag_sizes_g) as sizes
        from int_current_catalog where not is_wholesale and roaster_id in (?, ?) group by all
    )
    select x.title, x.price, x.sizes, y.price, y.sizes
    from c x join c y on x.nt = y.nt and x.roaster_id = ? and y.roaster_id = ? order by x.nt limit 12
    """
    shared = con.execute(q, [p["a"], p["b"], p["a"], p["b"]]).fetchall()
    only = """
    select any_value(title) from int_current_catalog
    where not is_wholesale and roaster_id = ? and trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) not in (
        select trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) from int_current_catalog where roaster_id = ?)
    group by trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) order by 1 limit 5
    """
    lines = [
        f"Store A: {store(p['a'])}; {p['na']} listed coffees",
        f"Store B: {store(p['b'])}; {p['nb']} listed coffees",
        f"Titles both list: {p['shared']} (showing up to 12; A's price and bag sizes | B's):",
    ]
    for t, pa, sa, pb, sb in shared:
        lines.append(f"  - {t} | A ${pa} {sa} | B ${pb} {sb}")
    for side, r, other in (("A", p["a"], p["b"]), ("B", p["b"], p["a"])):
        rows = [x[0] for x in con.execute(only, [r, other]).fetchall()]
        lines.append(f"Some titles only {side} lists: {rows if rows else 'none'}")
    return "\n".join(lines)


def rule_verdict(p: dict, rule: str) -> bool:
    """Candidate rules: '<feature>>=<c>,shared>=<s>[,ratio>=<r>]', e.g. 'cont>=0.8,shared>=3'; 'none' (main: no
    rule) flags nothing."""
    if rule == "none":
        return False
    ok = True
    for term in rule.split(","):
        k, v = term.split(">=")
        key = {"ratio": "size_ratio"}.get(k, k)
        ok = ok and p[key] >= float(v)
    return ok


def cmd_predict(data: Path, rule: str, out: str) -> None:
    items = json.loads((data / "dupes_items.json").read_text())
    Path(out).write_text(json.dumps({"rule": rule, "pred": {i["id"]: rule_verdict(i, rule) for i in items}}))


def cmd_score(data: Path, labels: Path, which: str, preds: list[str], examples: int) -> None:
    items = {i["id"]: i for i in json.loads((data / "dupes_items.json").read_text())}
    sizes = json.loads((data / "dupes_strata.json").read_text())
    ref = reference(labels, "dupes")
    for path in preds:
        pr = json.loads(Path(path).read_text())
        pred = pr["pred"]
        ids = [k for k, i in items.items() if i["split"] == which]
        flagged = [k for k in ids if pred[k]]
        tp = sum(ref[k] == "duplicate" for k in flagged)
        wrong = sum(ref[k] == "distinct" for k in flagged)
        pos = [k for k in ids if ref[k] == "duplicate"]
        caught = sum(pred[k] for k in pos)
        # Recall weighted by stratum size (S3 is a sample of its stratum).
        w = {s: sizes[s] / max(1, sum(items[k]["stratum"] == s for k in items)) for s in sizes}
        wpos = sum(w[items[k]["stratum"]] for k in pos)
        wcaught = sum(w[items[k]["stratum"]] for k in pos if pred[k])
        print(f"[{which}] {pr['rule']}: flagged {len(flagged)}")
        print(f"  precision (duplicate among flagged) {fmt(tp, len(flagged))}")
        print(f"  confidently wrong (distinct among flagged) {fmt(wrong, len(flagged))}")
        print(f"  recall over labelled duplicates {fmt(caught, len(pos))}; stratum-weighted {wcaught / max(wpos, 1):.3f}")
        if examples and which == "train":
            for k in [k for k in ids if pred[k] != (ref[k] == "duplicate")][:examples]:
                i = items[k]
                print(f"    {ref[k]:9} pred={pred[k]} {k} shared={i['shared']} rare={i['rare']} "
                      f"cont={i['cont']:.2f} rcont={i['rcont']:.2f} ratio={i['size_ratio']:.2f}")


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "items":
        cmd_items(sys.argv[2], Path(sys.argv[3]))
    elif cmd == "predict":
        cmd_predict(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    elif cmd == "score":
        ex = int(sys.argv[sys.argv.index("--examples") + 1]) if "--examples" in sys.argv else 0
        args = [a for a in sys.argv[5:] if a != "--examples" and not a.isdigit()]
        cmd_score(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], args, ex)


if __name__ == "__main__":
    main()
