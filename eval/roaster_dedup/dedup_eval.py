"""One store, one roaster: do two roaster ids serve the same store? (eval/roaster_dedup/README.md)

    python dedup_eval.py items   <main.duckdb> <data-dir> <eval-dir>   # strata, split, label inputs, keys
    python dedup_eval.py predict <db.duckdb> <data-dir> <name> <out.json>  # a build's verdict per item
    python dedup_eval.py score   <data-dir> <labels-dir> <set> <pred.json>...   # totals and Wilson intervals
    python dedup_eval.py agreement <labels-dir> <set>                      # labelers A and B

`items` reads main's build only (the evidence never shows a rule's verdict: main has no alias status). It writes
the label inputs (store facts and catalog titles from the private lake) to <data-dir>, outside git, and the item
keys to <eval-dir>: items/<set>_items.json for train and validation, sealed/test_keys.txt for test. `predict`
calls a pair the same store when the build gives both ids the same canonical_roaster_id (main has no such column:
it calls every pair two stores).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path

import duckdb

from coffeedata import registry

SALT = "coffeedata-roaster-dedup-2026-11-10"
SETS = ("train", "validation", "test")
LABELS = ("same_store", "different_store", "unsure")
SEED = 20261110  # the samples of the sampled strata
SAMPLE = {"C2": 60, "N1": 60, "R1": 40}

# The change's own host key (dbt/macros/roaster_alias.sql, as registry renders it for the crawl). The split was drawn
# with the round-1 expression (no scheme, path or leading-dot handling); on the 11-09 lake both give every roaster
# the same key, so the sets are unchanged (README, Split).
HOST_KEY = registry.alias_macros().host_key("host")
NT = "trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g'))"


def bucket(key: str) -> int:
    return int(hashlib.sha256(f"{SALT}:{key}".encode()).hexdigest()[:8], 16) % 100


def split_of(group_key: str) -> str:
    """60/20/20 by a salted hash of the group key: a roaster's host key (HOST_KEY), so every id of one store, and
    all of one roaster's pairs, sit on one set."""
    n = bucket(group_key)
    return "train" if n < 60 else "validation" if n < 80 else "test"


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def fmt(k: float, n: float) -> str:
    if n <= 0:
        return "n/a (0)"
    lo, hi = wilson(k, n)
    return f"{k / n:.3f} ({k:g}/{n:g}; {lo:.3f}-{hi:.3f})"


def stem(host: str) -> str:
    """The distinctive part of a host's first label (eval/dq's rule): 'blindtigercafe.com' -> 'blindtiger'."""
    base = re.sub(r"^www\.", "", host.lower()).split(".myshopify.com")[0].split(".")[0]
    for w in (
        "coffeeroasters",
        "coffeeroasting",
        "roasters",
        "roastery",
        "roasting",
        "coffeeco",
        "coffee",
        "cafe",
    ):
        base = base.replace(w, "")
    return re.sub(r"[^a-z0-9]", "", base)


def name_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("coffee", "").replace("roasters", ""))


def similar(a: str, b: str, na: str | None, nb: str | None) -> bool:
    sa, sb = stem(a), stem(b)
    by_host = len(sa) >= 4 and len(sb) >= 4 and (sa == sb or sa.startswith(sb) or sb.startswith(sa))
    ka, kb = name_key(na), name_key(nb)
    return by_host or (len(ka) >= 4 and ka == kb)


def _pairs(con: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[str, str]]]:
    """The strata, each a list of (a, b) with a < b. Every pair is in one stratum, the first that takes it."""
    con.execute(
        f"create temp table h as select roaster_id, status, roaster_name, city, state, {HOST_KEY} as k from dim_roaster"
    )
    con.execute(f"""
        create temp table t as select distinct roaster_id, {NT} as nt from int_current_catalog
        where not is_wholesale and title is not null and {NT} <> ''""")
    con.execute("""
        create temp table ov as
        with df as (select nt, count(*) as d from t group by nt), n as (select roaster_id, count(*) as n from t group by 1),
        p as (select a.roaster_id as a, b.roaster_id as b, count(*) as shared, count(*) filter (where df.d <= 3) as rare
              from t a join t b on a.nt = b.nt and a.roaster_id < b.roaster_id join df on df.nt = a.nt group by all)
        select p.*, p.shared / least(na.n, nb.n) as cont from p join n na on na.roaster_id = p.a join n nb on nb.roaster_id = p.b""")
    q = con.execute
    # H1: one host key, both active: the change's merges. H2: one host key, any other status (parked, dead).
    h1 = q("""select a.roaster_id, b.roaster_id from h a join h b on a.k = b.k and a.roaster_id < b.roaster_id
              where a.status = 'active' and b.status = 'active' order by all""").fetchall()
    h2 = q("""select a.roaster_id, b.roaster_id from h a join h b on a.k = b.k and a.roaster_id < b.roaster_id
              where not (a.status = 'active' and b.status = 'active') order by all""").fetchall()
    # Different host keys from here on. C1: at least 2 shared rare titles (listed by <= 3 roasters): rebrands
    # that don't redirect, and white-label stores. C2: at least 3 shared titles covering half the smaller list.
    diff = "join h ha on ha.roaster_id = ov.a join h hb on hb.roaster_id = ov.b where ha.k <> hb.k"
    c1 = q(f"select a, b from ov {diff} and rare >= 2 order by all").fetchall()
    c2 = q(f"select a, b from ov {diff} and rare < 2 and shared >= 3 and cont >= 0.5 order by all").fetchall()
    # N1: hosts or names that are variants of each other (eval/dq's rule), both with a catalog now.
    act = q(
        "select roaster_id, roaster_name, k from h where status = 'active' order by roaster_id"
    ).fetchall()
    n1 = []
    by_stem: dict[str, list] = {}
    for r in act:
        by_stem.setdefault(stem(r[0])[:4], []).append(r)
    by_name: dict[str, list] = {}
    for r in act:
        if len(name_key(r[1])) >= 4:
            by_name.setdefault(name_key(r[1]), []).append(r)
    cand = set()
    for group in list(by_stem.values()) + list(by_name.values()):
        for i, x in enumerate(group):
            for y in group[i + 1 :]:
                a, b = sorted((x, y))
                if a[2] != b[2] and similar(a[0], b[0], a[1], b[1]):
                    cand.add((a[0], b[0]))
    n1 = sorted(cand)
    # R1: two active stores in the same town with different host keys: what a merge must not take.
    r1 = q("""select a.roaster_id, b.roaster_id from h a join h b
              on a.city = b.city and a.state = b.state and a.roaster_id < b.roaster_id and a.k <> b.k
              where a.status = 'active' and b.status = 'active' order by all""").fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for name, ps in (("H1", h1), ("H2", h2), ("C1", c1), ("C2", c2), ("N1", n1), ("R1", r1)):
        out[name] = [p for p in ps if p not in seen]
        seen.update(out[name])
    return out


def cmd_items(db: str, data: Path, eval_dir: Path) -> None:
    con = duckdb.connect(db, read_only=True)
    if "canonical_roaster_id" in {r[0] for r in con.execute("describe dim_roaster").fetchall()}:
        raise SystemExit("items are drawn from main's build, whose evidence carries no alias verdict")
    strata = _pairs(con)
    # Each roaster's set is its host key's (aliases of one store always share a set); a pair is an item only when
    # both its roasters are in the same set, so no roaster is ever on two sets. Straddling pairs can't be held out
    # and are left out of the eval; the strata's sizes below count the eligible pairs only.
    key = dict(con.execute(f"select roaster_id, {HOST_KEY} from dim_roaster").fetchall())
    side = {r: split_of(k) for r, k in key.items()}
    eligible = {n: [p for p in ps if side[p[0]] == side[p[1]]] for n, ps in strata.items()}
    sizes = {n: {s: sum(side[p[0]] == s for p in ps) for s in SETS} for n, ps in eligible.items()}
    rng = random.Random(SEED)
    chosen: dict[str, list[tuple[str, str]]] = {n: [] for n in eligible}
    for n, ps in eligible.items():
        for s in (
            SETS
        ):  # each set's sample drawn from that set's eligible pairs, in proportion to the 60/20/20 split
            mine = [p for p in ps if side[p[0]] == s]
            k = (
                round(SAMPLE[n] * {"train": 0.6, "validation": 0.2, "test": 0.2}[s])
                if n in SAMPLE
                else len(mine)
            )
            chosen[n] += rng.sample(mine, k) if len(mine) > k else mine
    items = []
    for name, ps in chosen.items():
        for a, b in sorted(ps):
            items.append({"id": f"{a}|{b}", "a": a, "b": b, "stratum": name, "split": side[a]})
    drawn = {n: {s: sum(i["split"] == s for i in items if i["stratum"] == n) for s in SETS} for n in chosen}
    st = {"population": {n: len(ps) for n, ps in strata.items()}, "eligible": sizes, "drawn": drawn}
    data.mkdir(parents=True, exist_ok=True)
    (data / "items.json").write_text(json.dumps(items, indent=1))
    (data / "strata.json").write_text(json.dumps(st))
    for s in SETS:
        rows = [{"id": i["id"], "evidence": evidence(con, i["a"], i["b"])} for i in items if i["split"] == s]
        random.Random(f"{SEED}:{s}").shuffle(rows)  # no stratum order for a labeler to read
        (data / f"inputs_{s}.json").write_text(json.dumps(rows, indent=1))
    (eval_dir / "items").mkdir(parents=True, exist_ok=True)
    (eval_dir / "sealed").mkdir(parents=True, exist_ok=True)
    for s in ("train", "validation"):
        keep = [{k: i[k] for k in ("id", "stratum", "split")} for i in items if i["split"] == s]
        (eval_dir / "items" / f"{s}_items.json").write_text(json.dumps(keep, indent=1) + "\n")
    test = sorted(f"{i['id']}\t{i['stratum']}" for i in items if i["split"] == "test")
    (eval_dir / "sealed" / "test_keys.txt").write_text("\n".join(test) + "\n")
    (eval_dir / "strata.json").write_text(json.dumps(st, indent=1) + "\n")
    print(json.dumps(st))
    for s in SETS:
        c = Counter(i["stratum"] for i in items if i["split"] == s)
        print(s, sum(c.values()), dict(sorted(c.items())))


def evidence(con: duckdb.DuckDBPyConnection, a: str, b: str) -> str:
    def store(r: str) -> str:
        row = con.execute(
            "select roaster_name, city, state, platform, host, coffees_listed from dim_roaster where roaster_id = ?",
            [r],
        ).fetchone()
        return (
            f"id {r}; name {row[0]!r}; town {row[1] or '?'}, {row[2] or '?'}; platform {row[3] or '?'}; "
            f"its site resolves to host {row[4]}; {row[5]} coffees listed now"
        )

    shared = con.execute(
        f"""
        with c as (
            select roaster_id, {NT} as nt, any_value(title) as title, min(min_price_usd) as price,
                   any_value(bag_sizes_g) as sizes
            from int_current_catalog where not is_wholesale and roaster_id in (?, ?) group by all
        )
        select x.title, x.price, x.sizes, y.price, y.sizes
        from c x join c y on x.nt = y.nt and x.roaster_id = ? and y.roaster_id = ? order by x.nt limit 12""",
        [a, b, a, b],
    ).fetchall()
    n_shared = con.execute(
        f"""select count(distinct x.nt) from (select {NT} as nt from int_current_catalog where not is_wholesale and roaster_id = ?) x
            join (select {NT} as nt from int_current_catalog where not is_wholesale and roaster_id = ?) y using (nt)""",
        [a, b],
    ).fetchone()[0]
    only = f"""
        select any_value(title) from int_current_catalog
        where not is_wholesale and roaster_id = ? and {NT} not in (
            select {NT} from int_current_catalog where roaster_id = ? and title is not null)
        group by {NT} order by 1 limit 5"""
    lines = [
        f"Store A: {store(a)}",
        f"Store B: {store(b)}",
        f"Titles both list now: {n_shared} (up to 12 shown; A's lowest price and bag sizes | B's):",
    ]
    for t, pa, sa, pb, sb in shared:
        lines.append(f"  - {t} | A ${pa} {sa} | B ${pb} {sb}")
    for side, r, other in (("A", a, b), ("B", b, a)):
        rows = [x[0] for x in con.execute(only, [r, other]).fetchall()]
        lines.append(f"Some titles only {side} lists: {rows if rows else 'none'}")
    return "\n".join(lines)


def cmd_predict(db: str, data: Path, name: str, out: str, which: str | None = None) -> None:
    con = duckdb.connect(db, read_only=True)
    cols = {r[0] for r in con.execute("describe dim_roaster").fetchall()}
    canon = (
        dict(con.execute("select roaster_id, canonical_roaster_id from dim_roaster").fetchall())
        if "canonical_roaster_id" in cols
        else {}
    )
    items = json.loads((data / "items.json").read_text())
    if which:
        items = [i for i in items if i["split"] == which]
    pred = {i["id"]: bool(canon) and canon.get(i["a"], i["a"]) == canon.get(i["b"], i["b"]) for i in items}
    Path(out).write_text(json.dumps({"rule": name, "pred": pred}, indent=0, sort_keys=True) + "\n")
    print(name, "same_store", sum(pred.values()), "of", len(pred))


def _labels(path: Path) -> dict[str, str]:
    return {x["id"]: x["label"] for x in json.loads(path.read_text())["labels"]}


def _file(labels: Path, which: str, labeler: str) -> Path:
    """labels/<set>_<X>.json for train and validation; sealed/test_<X>.json for test (covered by SHA256SUMS)."""
    return labels / f"{which}_{labeler}.json"


def reference(labels: Path, which: str) -> dict[str, str]:
    """A and B where they agree, C (the tiebreak) where they don't."""
    a, b = (_labels(_file(labels, which, x)) for x in "AB")
    c_path = _file(labels, which, "C")
    c = _labels(c_path) if c_path.exists() else {}
    if set(a) != set(b):
        raise SystemExit("labelers A and B labelled different items")
    out = {}
    for k in a:
        if a[k] not in LABELS or b[k] not in LABELS:
            raise SystemExit(f"{k}: a label outside {LABELS}")
        if a[k] == b[k]:
            out[k] = a[k]
        elif k in c:
            out[k] = c[k]
        else:
            raise SystemExit(f"{k}: A and B disagree and there is no tiebreak label")
    return out


def cmd_agreement(labels: Path, which: str) -> None:
    a, b = (_labels(_file(labels, which, x)) for x in "AB")
    same = sum(a[k] == b[k] for k in a)
    print(
        f"[{which}] A/B agree on {fmt(same, len(a))}; disagreements {dict(Counter((a[k], b[k]) for k in a if a[k] != b[k]))}"
    )


def cmd_score(data: Path, labels: Path, which: str, preds: list[str]) -> None:
    """Totals only: precision, confidently wrong, and recall over the labelled same-store pairs, each with a Wilson
    95% interval, and recall weighted by stratum size (C2, N1 and R1 are samples of their strata)."""
    items = {i["id"]: i for i in json.loads((data / "items.json").read_text()) if i["split"] == which}
    st = json.loads((data / "strata.json").read_text())
    w = {n: st["eligible"][n][which] / max(1, st["drawn"][n][which]) for n in st["eligible"]}
    ref = reference(labels, which)
    if set(ref) != set(items):
        raise SystemExit(f"[{which}] labels cover {len(ref)} of {len(items)} items")
    print(f"[{which}] items {len(items)}; reference {dict(Counter(ref.values()))}")
    for path in preds:
        pr = json.loads(Path(path).read_text())
        pred = pr["pred"]
        flagged = [k for k in items if pred[k]]
        right = sum(ref[k] == "same_store" for k in flagged)
        wrong = sum(ref[k] == "different_store" for k in flagged)
        pos = [k for k in items if ref[k] == "same_store"]
        caught = sum(pred[k] for k in pos)
        wpos = sum(w[items[k]["stratum"]] for k in pos)
        wcaught = sum(w[items[k]["stratum"]] for k in pos if pred[k])
        unflagged = [k for k in items if not pred[k]]
        missed = sum(ref[k] == "same_store" for k in unflagged)
        print(f"  {pr['rule']}: called same store {len(flagged)}")
        print(f"    precision (same_store among called)        {fmt(right, len(flagged))}")
        print(f"    confidently wrong (different among called) {fmt(wrong, len(flagged))}")
        print(
            f"    recall (called among same_store)           {fmt(caught, len(pos))}; stratum-weighted {wcaught / wpos if wpos else math.nan:.3f}"
        )
        print(f"    missed (same_store among not called)       {fmt(missed, len(unflagged))}")


def main() -> None:
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "items":
        cmd_items(args[0], Path(args[1]), Path(args[2]))
    elif cmd == "predict":
        cmd_predict(args[0], Path(args[1]), args[2], args[3], args[4] if len(args) > 4 else None)
    elif cmd == "score":
        cmd_score(Path(args[0]), Path(args[1]), args[2], args[3:])
    elif cmd == "agreement":
        cmd_agreement(Path(args[0]), args[1])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
