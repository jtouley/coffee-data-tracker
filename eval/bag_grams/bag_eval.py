"""Bag weight (grams) evaluation: see README.md.

    python bag_eval.py build   <lake-dir> <data-dir>
    python bag_eval.py predict <data-dir> <name>          # with PYTHONPATH at the src whose rule <name> is
    python bag_eval.py coverage <data-dir> <set> <name>...
    python bag_eval.py masked  <data-dir> <set> <name>
    python bag_eval.py examples <data-dir> train <a> <b> [N]   # train only
    python bag_eval.py sample  <data-dir> <set> main <branch>
    python bag_eval.py seal    <data-dir> <set> main <branch>
    python bag_eval.py packet  <data-dir> <set>
    python bag_eval.py agree   <set> <labels_A.json> <labels_B.json>
    python bag_eval.py settle  <set> <labels_A.json> <labels_B.json> <labels_C.json> <labels_ref.json>
    python bag_eval.py score   <data-dir> <set> <labels_ref.json> main <branch> [--final]

Population (build): every available, priced variant of a current retail coffee in the 2026-11-02 snapshot. A coffee
is current retail when the 11-02 release's public_coffee lists it (is_listed), it was last seen 11-02, its
catalog_status is available or preorder and it isn't wholesale. One variant per coffee is the labelled item
(the lowest sha256(SALT + coffee_id + variant_id)); coverage is over all of a coffee's variants.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

SALT = "coffeedata-bag-grams-eval-2026-11-10"
RELEASE = "releases/run_date=2026-11-02/build=20261104T041920-5060ad"
SNAPSHOT = "snapshots/run_date=2026-11-02"
SETS = ("train", "validation", "test")
# Per set: how many items to draw from each stratum (stratum()). F: main has no grams and the branch fills them;
# C: main has grams and the branch changes them (a misread corrected, or a pack's single-bag weight dropped);
# A: neither has grams (the branch abstains); K: main has grams and the branch keeps them.
SAMPLE = {
    "train": {"F": 100, "C": 50, "A": 50, "K": 50},
    "validation": {"F": 150, "C": 75, "A": 75, "K": 75},
    "test": {"F": 200, "C": 100, "A": 100, "K": 100},
}
PRICE_BAND = (0.5, 100.0)  # dim_coffee's plausible $ per 100 g
BAG_BAND = (150, 2500)  # dim_coffee's bag sizes for price_per_100g


def h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def split_of(group: str) -> str:
    n = h(f"{SALT}:{group}") % 100
    return "train" if n < 60 else "validation" if n < 80 else "test"


def group_key(host: str | None, roaster_id: str) -> str:
    """The roaster group: the store's resolved host, lower-cased without www/shop/store, so alias ids of one store
    (the duplicate roaster ids of the lake review) share a side. Falls back to roaster_id."""
    import re

    if not host:
        return roaster_id
    return re.sub(r"^(www\d?|shop|store)\.", "", host.strip().lower())


def cmd_build(lake: Path, data: Path) -> None:
    import duckdb

    data.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    rel = lake / RELEASE
    q = f"""
    with pc as (
        select coffee_id, roaster_id from read_parquet('{rel}/public_coffee.parquet', hive_partitioning=false)
        where is_listed and last_seen = date '2026-11-02' and catalog_status in ('available', 'preorder')
          and not is_wholesale
    ),
    r as (select roaster_id, host from read_parquet('{rel}/dim_roaster.parquet', hive_partitioning=false)),
    s as (
        select s.roaster_id || ':' || s.product_id as coffee_id, s.platform, s.title, s.handle, s.url,
               s.product_type, s.tags, s.body_hash,
               [struct_pack(variant_id := v.variant_id, title := v.title, price := v.price_usd, grams := v.grams,
                            available := v.available) for v in s.variants] as variants
        from read_parquet('{lake / SNAPSHOT}/*.parquet', hive_partitioning=false) s
    ),
    b as (
        select body_hash, any_value(body_text) as body_text
        from read_parquet('{lake}/bodies/*/*.parquet', hive_partitioning=false, union_by_name=true)
        group by body_hash
    )
    select pc.coffee_id, pc.roaster_id, r.host, s.* exclude (coffee_id, body_hash), b.body_text
    from pc join s using (coffee_id) left join r using (roaster_id) left join b using (body_hash)
    order by pc.coffee_id
    """
    rows = [dict(zip([d[0] for d in con.description], t, strict=True)) for t in con.execute(q).fetchall()]
    out = []
    for r in rows:
        priced = [v for v in r["variants"] if v["available"] is not False and (v["price"] or 0) > 0]
        if not priced:
            continue  # no available price: not a weight question (Square Online ranges, #87)
        item = min(priced, key=lambda v: h(f"{SALT}:{r['coffee_id']}:{v['variant_id']}"))
        group = group_key(r.pop("host"), r["roaster_id"])
        out.append(r | {"group": group, "set": split_of(group), "item_variant_id": item["variant_id"]})
    (data / "population.json").write_text(json.dumps(out, sort_keys=True, default=str))
    c = Counter(r["set"] for r in out)
    g = {s: len({r["group"] for r in out if r["set"] == s}) for s in SETS}
    print({s: (c[s], g[s]) for s in SETS}, "coffees, roaster groups")


def load(data: Path, split: str | None = None) -> list[dict]:
    rows = json.loads((data / "population.json").read_text())
    return [r for r in rows if split is None or r["set"] == split]


def cmd_predict(data: Path, name: str) -> None:
    """Each variant's grams under the rule on PYTHONPATH: coffeedata.bag_weight.product_grams when it exists
    (the branch), else the stored grams (main: dbt reads the crawler's grams as stored)."""
    try:
        from coffeedata.bag_weight import product_grams
    except ImportError:
        product_grams = None
    out = {}
    for r in load(data):
        stored = [v["grams"] for v in r["variants"]]
        if product_grams is None:
            grams = stored
        else:
            # candidates compared on validation: the option titles only, then the product title, then the description
            title = "" if name.endswith("-options") else r["title"]
            body = "" if name.endswith(("-options", "-title")) else r["body_text"]
            grams = product_grams(title, body, [(v["title"], v["price"], v["grams"]) for v in r["variants"]])
        out[r["coffee_id"]] = {v["variant_id"]: g for v, g in zip(r["variants"], grams, strict=True)}
    (data / f"pred_{name}.json").write_text(json.dumps(out, sort_keys=True))
    print(len(out), "coffees predicted as", name)


def pred(data: Path, name: str) -> dict:
    return json.loads((data / f"pred_{name}.json").read_text())


def p100(r: dict, grams: dict) -> float | None:
    """dim_coffee's price_per_100g over the coffee's variants with these grams."""
    vals = []
    for v in r["variants"]:
        g, p = grams.get(v["variant_id"]), v["price"]
        if g and p and p > 0 and v["available"] is not False and BAG_BAND[0] <= g <= BAG_BAND[1]:
            x = p / g * 100
            if PRICE_BAND[0] <= x <= PRICE_BAND[1]:
                vals.append(round(x, 2))
    return min(vals) if vals else None


def cmd_coverage(data: Path, split: str, names: list[str]) -> None:
    """Share of coffees with a price_per_100g, and of labelled-item variants with grams, per platform."""
    rows = load(data, split)
    preds = {n: pred(data, n) for n in names}
    by: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for key in (r["platform"], "ALL"):
            by[key]["n"] += 1
            for n in names:
                gr = preds[n][r["coffee_id"]]
                by[key][f"{n}_p100"] += p100(r, gr) is not None
                by[key][f"{n}_item_g"] += gr.get(r["item_variant_id"]) is not None
    hdr = ["platform", "coffees"] + [f"{n} p100" for n in names] + [f"{n} item grams" for n in names]
    print(" | ".join(hdr))
    for k in sorted(by, key=lambda k: (k == "ALL", -by[k]["n"])):
        c = by[k]
        cells = [f"{c[f'{n}_p100']} ({100 * c[f'{n}_p100'] / c['n']:.1f}%)" for n in names]
        cells += [f"{c[f'{n}_item_g']} ({100 * c[f'{n}_item_g'] / c['n']:.1f}%)" for n in names]
        print(" | ".join([k, str(c["n"])] + cells))


def cmd_masked(data: Path, split: str, name: str, show: int = 0) -> None:
    """Label-free check of the product-title and description rules: on coffees whose every variant has a stored
    weight, hide the options' titles and weights and ask the rules again. Totals only: agree (within 2%), wrong,
    abstain, over variants. name: <x>-title (title rule only) or anything else (title, then description)."""
    from coffeedata.bag_weight import product_grams
    from coffeedata.models import parse_grams

    c: Counter = Counter()
    for r in load(data, split):
        vs = r["variants"]
        # stored weights read from the options' own titles (not Shopify's shipping grams): the reference here
        if not vs or any(v["grams"] is None or parse_grams(v["title"] or "") != v["grams"] for v in vs):
            continue
        body = "" if name.endswith("-title") else r["body_text"]
        got = product_grams(r["title"], body, [("", v["price"], None) for v in vs])
        for v, g in zip(vs, got, strict=True):
            k = "abstain" if g is None else "agree" if abs(g - v["grams"]) <= 0.02 * v["grams"] else "wrong"
            c[k] += 1
            if show and k == "wrong" and split == "train" and c["shown"] < show:
                c["shown"] += 1
                print("  ", r["title"][:70], "|", repr(v["title"][:40]), v["grams"], "->", g)
    c.pop("shown", None)
    n = sum(c.values())
    print(name, split, dict(c), {k: f"{100 * v / n:.2f}%" for k, v in c.items()})


def cmd_examples(data: Path, split: str, a: str, b: str, n: int = 40) -> None:
    if split != "train":
        sys.exit("examples are read from train only")
    pa_, pb = pred(data, a), pred(data, b)
    rows = [r for r in load(data, split) if pa_[r["coffee_id"]] != pb[r["coffee_id"]]]
    random.Random(1).shuffle(rows)
    for r in rows[:n]:
        print(r["platform"], "|", r["title"], "| handle:", r["handle"])
        for v in r["variants"]:
            print(
                "   ",
                repr(v["title"])[:60],
                v["price"],
                pa_[r["coffee_id"]][v["variant_id"]],
                "->",
                pb[r["coffee_id"]][v["variant_id"]],
            )


def stratum(r: dict, m: dict, b: dict) -> str:
    vid = r["item_variant_id"]
    gm, gb = m[r["coffee_id"]].get(vid), b[r["coffee_id"]].get(vid)
    if gm is not None:
        return "K" if gb == gm else "C"  # C: the branch changes a stored weight (none expected)
    return "F" if gb is not None else "A"


def strata(data: Path, split: str, main: str, branch: str) -> dict[str, list[str]]:
    m, b = pred(data, main), pred(data, branch)
    out: dict[str, list[str]] = defaultdict(list)
    for r in load(data, split):
        out[stratum(r, m, b)].append(r["coffee_id"])
    return {k: sorted(v) for k, v in out.items()}


def cmd_sample(data: Path, split: str, main: str, branch: str) -> None:
    st = strata(data, split, main, branch)
    rng = random.Random(f"{SALT}:{split}")
    drawn = []
    for k in sorted(st):
        ids = list(st[k])
        rng.shuffle(ids)
        drawn += [{"coffee_id": i, "stratum": k} for i in ids[: SAMPLE[split][k]]]
    sizes = {k: len(v) for k, v in st.items()}
    here = Path(__file__).parent
    (here / f"items_{split}.json").write_text(
        json.dumps(
            {"set": split, "stratum_sizes": sizes, "items": sorted(drawn, key=lambda d: d["coffee_id"])},
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(split, "strata", sizes, "drawn", Counter(d["stratum"] for d in drawn))


def cmd_seal(data: Path, split: str, names: list[str]) -> None:
    """Each rule's grams for the set's labeled options, written here (pred_<name>_<set>.json: ids and grams only)
    before any label exists, so SHA256SUMS can pin them."""
    items = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
    pop = {r["coffee_id"]: r for r in load(data, split)}
    for name in names:
        pr = pred(data, name)
        out = {
            it["coffee_id"]: pr[it["coffee_id"]].get(pop[it["coffee_id"]]["item_variant_id"]) for it in items
        }
        p = Path(__file__).parent / f"pred_{name}_{split}.json"
        p.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
        print(p.name, len(out))


def cmd_packet(data: Path, split: str) -> None:
    """The labeller's view: the listing's text, every option with its price, which option to label; no stratum,
    no prediction, shuffled. Written to the data dir (listing text stays out of git)."""
    items = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
    pop = {r["coffee_id"]: r for r in load(data, split)}
    out = []
    for it in items:
        r = pop[it["coffee_id"]]
        out.append(
            {
                "coffee_id": r["coffee_id"],
                "url": r["url"],
                "title": r["title"],
                "handle": r["handle"],
                "product_type": r["product_type"],
                "tags": r["tags"],
                "description": (r["body_text"] or "")[:4000],
                "options": [
                    {
                        "variant_id": v["variant_id"],
                        "option": v["title"],
                        "price": v["price"],
                        "available": v["available"],
                    }
                    for v in r["variants"]
                ],
                "label_this_option": r["item_variant_id"],
            }
        )
    random.Random(f"{SALT}:packet:{split}").shuffle(out)
    p = data / f"to_label_{split}.json"
    p.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(p, len(out))


def same(a: int | str, b: int | str) -> bool:
    """Two labels agree: both the same word, or grams within 2% of each other."""
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(a - b) <= 0.02 * max(a, b)


def cmd_agree(split: str, a: Path, b: Path) -> None:
    """Agreement of two independent labelers (totals), and the items a third must settle (to_settle_<set>.json,
    ids only, written next to the labels)."""
    la, lb = json.loads(a.read_text()), json.loads(b.read_text())
    items = [
        it["coffee_id"]
        for it in json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
    ]
    missing = [i for i in items if i not in la or i not in lb]
    if missing:
        sys.exit(f"{len(missing)} items lack a label, e.g. {missing[:3]}")
    diff = sorted(i for i in items if not same(la[i], lb[i]))
    lo, hi = wilson(len(items) - len(diff), len(items))
    print(
        f"{split}: agree {len(items) - len(diff)}/{len(items)} [{100 * lo:.1f}, {100 * hi:.1f}]; to settle {len(diff)}"
    )
    (a.parent / f"to_settle_{split}.json").write_text(json.dumps(diff, indent=1) + "\n")


def cmd_settle(split: str, a: Path, b: Path, c: Path, out: Path) -> None:
    """The reference: A where A and B agree, else C (the tiebreak labeled only the disagreements)."""
    la, lb, lc = (json.loads(p.read_text()) for p in (a, b, c))
    ref = {}
    for i in la:
        if same(la[i], lb[i]):
            ref[i] = la[i]
        elif i in lc:
            ref[i] = lc[i]
        else:
            sys.exit(f"{i}: A and B disagree and C has no label")
    out.write_text(json.dumps(ref, indent=1, sort_keys=True) + "\n")
    print(
        split,
        len(ref),
        "reference labels,",
        Counter(type(v).__name__ if not isinstance(v, str) else v for v in ref.values()),
    )


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - w) / d, (c + w) / d)


OUTCOMES = ("right", "wrong", "abstain_ok", "missed")


def outcome(g: int | None, label: int | str) -> str:
    """right: grams within 2% of the label's (unit rounding: 12 oz is 340 g); wrong: grams for an "unknown" label,
    or off by more than 2% (confidently wrong); abstain_ok: no grams, label "unknown"; missed: no grams, but the
    listing states a weight."""
    if g is None:
        return "abstain_ok" if label == "unknown" else "missed"
    if label == "unknown":
        return "wrong"
    return "right" if abs(g - int(label)) <= 0.02 * int(label) else "wrong"


def weighted(per: dict[str, list[float]], sizes: dict[str, int]) -> tuple[float, float, float]:
    """A stratum-weighted mean of per-item scores, with a normal 95% interval over the strata's variances."""
    total = sum(sizes[k] for k in per)
    est = var = 0.0
    for k, xs in per.items():
        if not xs:
            continue
        w, n = sizes[k] / total, len(xs)
        m = sum(xs) / n
        est += w * m
        var += w * w * (sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0) / n
    half = 1.96 * math.sqrt(var)
    return est, est - half, est + half


def cmd_score(data: Path, split: str, labels_file: Path, main: str, branch: str, final: bool) -> None:
    """Totals only. Per rule and stratum: right, wrong (confidently wrong), abstain_ok and missed, with Wilson 95%
    intervals; the stratum-weighted accuracy (right + abstain_ok), wrong share and covered-right share; and the
    paired difference branch - main on the same items. Items labeled "unclear" are left out and counted."""
    if split == "test" and not final:
        sys.exit("the test set is scored once, with --final")
    meta = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())
    labels = json.loads(labels_file.read_text())
    sizes, items = meta["stratum_sizes"], meta["items"]
    pop = {r["coffee_id"]: r for r in load(data, split)}
    preds = {name: pred(data, name) for name in (main, branch)}
    for name, pr in preds.items():  # the sealed predictions must be the ones scored
        sealed = Path(__file__).parent / f"pred_{name}_{split}.json"
        if sealed.exists():
            want = json.loads(sealed.read_text())
            got = {i: pr[i].get(pop[i]["item_variant_id"]) for i in want}
            if got != want:
                sys.exit(f"{name}'s predictions differ from {sealed.name}")
    res: dict[str, dict[str, list[str]]] = {name: defaultdict(list) for name in preds}
    unclear: Counter = Counter()
    for it in items:
        lab = labels.get(it["coffee_id"])
        if lab is None or lab == "unclear":
            unclear[it["stratum"]] += 1
            continue
        for name, pr in preds.items():
            g = pr[it["coffee_id"]].get(pop[it["coffee_id"]]["item_variant_id"])
            res[name][it["stratum"]].append(outcome(g, lab))
    print(
        f"{split}: {len(items)} items, unclear {sum(unclear.values())} {dict(unclear)}; stratum sizes {sizes}"
    )
    for name in preds:
        print(f"== {name}")
        for k in sorted(res[name]):
            xs = res[name][k]
            cells = []
            for o in OUTCOMES:
                lo, hi = wilson(xs.count(o), len(xs))
                cells.append(f"{o} {xs.count(o)}/{len(xs)} [{100 * lo:.1f}, {100 * hi:.1f}]")
            print(f"  {k}: " + "; ".join(cells))
        for label, ok in (
            ("accuracy", ("right", "abstain_ok")),
            ("wrong", ("wrong",)),
            ("right", ("right",)),
        ):
            est, lo, hi = weighted({k: [float(o in ok) for o in xs] for k, xs in res[name].items()}, sizes)
            print(f"  weighted {label}: {100 * est:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]")
    for label, ok in (("accuracy", ("right", "abstain_ok")), ("wrong", ("wrong",))):
        diff = {
            k: [float(b in ok) - float(m in ok) for m, b in zip(res[main][k], res[branch][k], strict=True)]
            for k in res[main]
        }
        est, lo, hi = weighted(diff, sizes)
        print(
            f"paired {label}, {branch} - {main}: {100 * est:+.1f} points [{100 * lo:+.1f}, {100 * hi:+.1f}]"
        )


def main() -> None:
    a = sys.argv[1:]
    cmd, data = a[0], Path(a[1]) if len(a) > 1 else None
    if cmd == "build":
        cmd_build(Path(a[1]), Path(a[2]))
    elif cmd == "predict":
        cmd_predict(data, a[2])
    elif cmd == "coverage":
        cmd_coverage(data, a[2], a[3:])
    elif cmd == "masked":
        cmd_masked(data, a[2], a[3], int(a[4]) if len(a) > 4 else 0)
    elif cmd == "examples":
        cmd_examples(data, a[2], a[3], a[4], int(a[5]) if len(a) > 5 else 40)
    elif cmd == "sample":
        cmd_sample(data, a[2], a[3], a[4])
    elif cmd == "seal":
        cmd_seal(data, a[2], a[3:])
    elif cmd == "packet":
        cmd_packet(data, a[2])
    elif cmd == "agree":
        cmd_agree(a[1], Path(a[2]), Path(a[3]))
    elif cmd == "settle":
        cmd_settle(a[1], Path(a[2]), Path(a[3]), Path(a[4]), Path(a[5]))
    elif cmd == "score":
        cmd_score(data, a[2], Path(a[3]), a[4], a[5], "--final" in a)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
