"""Bag weight (grams) evaluation: see README.md.

    python bag_eval.py build   <lake-dir> <data-dir>
    python bag_eval.py predict <data-dir> <name>          # with PYTHONPATH at the src whose rule <name> is
    python bag_eval.py coverage <data-dir> <set> <name>...
    python bag_eval.py masked  <data-dir> <set> <name>
    python bag_eval.py examples <data-dir> train <a> <b> [N]   # train only
    python bag_eval.py sample  <data-dir> <set> main <branch>
    python bag_eval.py changed <data-dir> <set> main <candidate>...   # round 3: every coffee the candidates change
    python bag_eval.py seal    <data-dir> <set> main <branch>
    python bag_eval.py fixture <data-dir> <out.jsonl> train validation test
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


# The second test set (test2) is drawn from the first test split's stores: the only stores none of whose listings
# was read while the rules were tuned. Its items are none of the first round's.
POPULATION_SET = {"test2": "test"}
SALT2 = "coffeedata-bag-grams-test2-2026-11-11"
SAMPLE["test2"] = {"F": 200, "C": 100, "A": 100, "K": 100}

# Round 3 (bag counts, review M1 of #136): the change touches about a hundred coffees, so its sets are a census of
# the coffees whose grams it changes (stratum D), one changed option each, rather than a stratified sample of all.
# train-3 and validation-3 take every changed coffee of their split; test3, drawn only after the rules are frozen,
# every changed coffee of the test split that no earlier round labeled.
POPULATION_SET |= {"train-3": "train", "validation-3": "validation", "test3": "test"}
SALT3 = "coffeedata-bag-grams-counts-2026-11-11"
TEST_SETS = ("test", "test2", "test3")


def item_vid(it: dict, r: dict) -> str:
    """The option an item labels: its own (round 3 picks a changed one), else the coffee's labeled option."""
    return it.get("variant_id") or r["item_variant_id"]


def load(data: Path, split: str | None = None) -> list[dict]:
    rows = json.loads((data / "population.json").read_text())
    if split and split.startswith("gate-"):  # a gate set is drawn from the whole population
        return rows
    split = POPULATION_SET.get(split, split) if split else split
    return [r for r in rows if split is None or r["set"] == split]


def cmd_predict(data: Path, name: str) -> None:
    """Each variant's grams under the rule on PYTHONPATH: coffeedata.bag_weight.product_grams when it exists
    (the branch), else the stored grams (main: dbt reads the crawler's grams as stored).

    A candidate's settings ride in its name, for comparing levers on train and choosing on validation:
    "<label>+u=<standard|drop|keep>+s=<title.tags.body.handle>+c=<multiply|block>+f=<0|1>" (the defaults are
    product_grams' own)."""
    try:
        from coffeedata.bag_weight import product_grams
    except ImportError:
        product_grams = None
    opts: dict = {}
    for part in name.split("+")[1:]:
        k, _, v = part.partition("=")
        if k == "u":
            opts["unsupported"] = v
        elif k == "s":
            opts["sources"] = tuple(x for x in v.split(".") if x)
        elif k == "c":
            opts["bag_counts"] = v
        elif k == "f":
            opts["count_fill"] = v == "1"
    out = {}
    for r in load(data):
        stored = [v["grams"] for v in r["variants"]]
        if product_grams is None:
            grams = stored
        else:
            options = [(v["title"], v["price"], v["grams"]) for v in r["variants"]]
            grams = product_grams(
                r["title"], r["body_text"], options, tags=r.get("tags"), handle=r.get("handle"), **opts
            )
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


def published(r: dict, grams: dict) -> tuple[float | None, str | None]:
    """dim_coffee's price_per_100g with these grams, and the option it comes from (the first, in the listing's order,
    of the options at that price per 100 g); (None, None) when nothing is published."""
    best: tuple[float, str] | None = None
    for v in r["variants"]:
        g, p = grams.get(v["variant_id"]), v["price"]
        if g and p and p > 0 and v["available"] is not False and BAG_BAND[0] <= g <= BAG_BAND[1]:
            x = round(p / g * 100, 2)
            if PRICE_BAND[0] <= p / g * 100 <= PRICE_BAND[1] and (best is None or x < best[0]):
                best = (x, v["variant_id"])
    return best if best else (None, None)


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
    salt = SALT2 if split == "test2" else SALT
    if split == "test2":  # none of the first round's items, in any set
        used = set()
        for old in ("train", "validation", "test"):
            used |= {
                it["coffee_id"]
                for it in json.loads((Path(__file__).parent / f"items_{old}.json").read_text())["items"]
            }
        st = {k: [i for i in v if i not in used] for k, v in st.items()}
    rng = random.Random(f"{salt}:{split}")
    drawn = []
    for k in sorted(st):
        ids = list(st[k])
        rng.shuffle(ids)
        drawn += [{"coffee_id": i, "stratum": k} for i in ids[: SAMPLE[split][k]]]
    sizes = {k: len(v) for k, v in st.items()}
    groups = {r["coffee_id"]: r["group"] for r in load(data, split)}
    for d in drawn:
        d["store"] = groups[d["coffee_id"]]
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


def cmd_changed(data: Path, split: str, main: str, candidates: list[str]) -> None:
    """Round 3's census: every coffee of the set's split whose grams any candidate changes against main (stratum D),
    each with one changed option to label: its round-1 labeled option if that one changes, else the changed option
    with the lowest sha256(SALT3:coffee_id:variant_id). test3 leaves out every coffee an earlier round labeled, and
    is drawn only once the rules are frozen (one candidate). Writes items_<set>.json here (ids only)."""
    here = Path(__file__).parent
    if split == "test3" and len(candidates) != 1:
        sys.exit("test3 is drawn for the frozen rules only: one candidate")
    pm, pcs = pred(data, main), [pred(data, c) for c in candidates]
    # an earlier round's reference label for the same option is reused, not labeled again ("label_from")
    earlier = POPULATION_SET[split] if split != "test3" else None
    reuse: dict[str, str] = {}
    if earlier:
        ref = json.loads((here / "labels" / f"labels_ref_{earlier}.json").read_text())
        reuse = {
            it["coffee_id"]: earlier
            for it in json.loads((here / f"items_{earlier}.json").read_text())["items"]
        }
        reuse = {c: e for c, e in reuse.items() if c in ref}
    used = set()
    if split == "test3":
        for old in ("test", "test2"):
            used |= {it["coffee_id"] for it in json.loads((here / f"items_{old}.json").read_text())["items"]}
    drawn, left_out = [], 0
    for r in load(data, split):
        cid = r["coffee_id"]
        changed = [
            v["variant_id"]
            for v in r["variants"]
            if any(pc[cid].get(v["variant_id"]) != pm[cid].get(v["variant_id"]) for pc in pcs)
        ]
        if not changed:
            continue
        if cid in used:
            left_out += 1
            continue
        vid = (
            r["item_variant_id"]
            if r["item_variant_id"] in changed
            else min(changed, key=lambda v: h(f"{SALT3}:{cid}:{v}"))
        )
        d = {"coffee_id": cid, "stratum": "D", "store": r["group"], "variant_id": vid}
        if cid in reuse and vid == r["item_variant_id"]:
            d["label_from"] = reuse[cid]
        drawn.append(d)
    (here / f"items_{split}.json").write_text(
        json.dumps(
            {
                "set": split,
                "stratum_sizes": {"D": len(drawn)},
                "items": sorted(drawn, key=lambda d: d["coffee_id"]),
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    st = Counter(d["store"] for d in drawn)
    print(
        f"{split}: {len(drawn)} changed coffees from {len(st)} stores (largest {max(st.values(), default=0)}); "
        f"{left_out} left out as labeled before; {sum('label_from' in d for d in drawn)} reuse an earlier label, "
        f"{sum('label_from' not in d for d in drawn)} to label"
    )


# The full-population gate (CLAUDE.md step 7): the share of all current coffees whose drawn option the rules get
# right (right, or rightly blank), at least GATE. See README "The full-population gate".
GATE = 0.95
GATE_SETS = (
    "validation",
    "test2",
)  # stratified over their whole split; train, which rules are tuned on, isn't one
# The eval's population: every current retail coffee of the 2026-11-02 snapshot (population.json, sha256 under "Sealed
# files" in README.md), a frozen snapshot, not the live catalogue. tests/test_bag_grams_accuracy.py reads it here.
POPULATION = 30_022


def full_population(ok_d: int, n_d: int, a_u: float, n_pop: int = POPULATION) -> float:
    """The gate's estimate: the D coffees counted exactly (ok_d of n_d right or rightly blank; an "unclear" label
    counts as not right), every other coffee at a_U, the accuracy GATE_SETS estimate for them."""
    return (ok_d + (n_pop - n_d) * a_u) / n_pop


def is_ok(g: int | None, label: int | str) -> bool:
    """Right, or rightly blank; an "unclear" label is neither (review L4 of #139)."""
    return label != "unclear" and outcome(g, label) in ("right", "abstain_ok")


LABELED = ("train", "validation", "test", "test2", "train-3", "validation-3", "test3")


def cmd_gate_d(data: Path, main: str, change: str, out: Path) -> None:
    """The coffees whose drawn option a change gives other grams (D), written to `out` (ids only). Every one must
    have a reference label for that option in some set (a census covers them), else the gate can't count them."""
    pm, pc = pred(data, main), pred(data, change)
    pop = load(data)
    d = sorted(
        r["coffee_id"]
        for r in pop
        if pm[r["coffee_id"]].get(r["item_variant_id"]) != pc[r["coffee_id"]].get(r["item_variant_id"])
    )
    labeled = _drawn_labels({r["coffee_id"]: r for r in pop})
    missing = [c for c in d if c not in labeled]
    if missing:
        sys.exit(f"{len(missing)} changed coffees have no label for their drawn option, e.g. {missing[:3]}")
    old = json.loads(out.read_text()) if out.exists() else []
    out.write_text(json.dumps(sorted(set(old) | set(d)), indent=1) + "\n")
    unclear = sum(labeled[c] == "unclear" for c in d)
    print(
        out.name, len(d), "changed coffees this round,", len(set(old) | set(d)), "in all;", unclear, "unclear"
    )


def _drawn_labels(pop: dict[str, dict]) -> dict[str, int | str]:
    """Every reference label made for a coffee's drawn option, in any set."""
    here = Path(__file__).parent
    out: dict[str, int | str] = {}
    for split in LABELED:
        ref = json.loads((here / "labels" / f"labels_ref_{split}.json").read_text())
        for it in json.loads((here / f"items_{split}.json").read_text())["items"]:
            r = pop[it["coffee_id"]]
            if item_vid(it, r) == r["item_variant_id"] and it["coffee_id"] in ref:
                out[it["coffee_id"]] = ref[it["coffee_id"]]
    return out


def cmd_gate(data: Path, names: list[str]) -> None:
    """The full-population estimate for each rule: D (gate_d.json) counted exactly from its labels, the rest
    estimated from GATE_SETS' items outside D (stratum-weighted, sets combined by their stratum sizes), weighted by
    D's and the rest's share of the population. Normal and store-clustered 95% intervals (D adds none)."""
    here = Path(__file__).parent
    pop = {r["coffee_id"]: r for r in load(data)}
    if len(pop) != POPULATION:
        sys.exit(f"population.json has {len(pop)} coffees, not the eval's {POPULATION}")
    d = set(json.loads((here / "gate_d.json").read_text()))
    labels = _drawn_labels(pop)
    for name in names:
        pr = pred(data, name)
        ok_d = sum(is_ok(pr[c].get(pop[c]["item_variant_id"]), labels[c]) for c in d)
        sizes: dict[str, int] = {}
        per: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for split in GATE_SETS:
            meta = json.loads((here / f"items_{split}.json").read_text())
            ref = json.loads((here / "labels" / f"labels_ref_{split}.json").read_text())
            for k, v in meta["stratum_sizes"].items():
                sizes[f"{split}:{k}"] = v
            for it in meta["items"]:
                c, lab = it["coffee_id"], ref.get(it["coffee_id"])
                if c in d or lab in (None, "unclear"):
                    continue
                g = pr[c].get(pop[c]["item_variant_id"])
                per[f"{split}:{it['stratum']}"].append(
                    (it.get("store") or pop[c]["group"], float(is_ok(g, lab)))
                )
        est, lo, hi = weighted({k: [x for _, x in v] for k, v in per.items()}, sizes)
        clo, chi = clustered(per, sizes)
        a, a_lo, a_hi, a_clo, a_chi = (
            100 * full_population(ok_d, len(d), u) for u in (est, lo, hi, clo, chi)
        )
        print(
            f"{name}: full population {a:.2f}%  normal [{a_lo:.2f}, {a_hi:.2f}]"
            f"  store-clustered [{a_clo:.2f}, {a_chi:.2f}]  (D: {ok_d}/{len(d)} right; rest"
            f" {100 * est:.2f}% from {sum(len(v) for v in per.values())} items)"
        )


# ---- The full-population gate (README "The full-population gate"; CLAUDE.md step 7) --------------------------------
# For each rules change, once its rules are frozen: a fresh sealed sample of coffees from the whole population,
# stratified by platform and disjoint from every set labeled before. What is scored is what dim_coffee publishes:
# each coffee's price_per_100g, through the grams of the option it comes from, under main's rules and the change's.
GATE_N = 500  # coffees per gate set
GATE_MIN = 40  # per platform, before the rest is shared by stratum size
GATE_ACCURACY = 0.95  # the change's weighted accuracy must reach this
GATE_RISE = (
    0.005  # and its weighted confidently wrong share may exceed main's by at most this (point estimate)
)
GATE_PREFIX = "gate-"  # gate sets are named gate-<change>, e.g. gate-139


def _labeled_coffees() -> set[str]:
    here = Path(__file__).parent
    out: set[str] = set()
    for f in here.glob("items_*.json"):
        out |= {it["coffee_id"] for it in json.loads(f.read_text())["items"]}
    return out


def cmd_gate_sample(data: Path, split: str, main: str, change: str) -> None:
    """Draw a gate set (items_<gate-...>.json, ids only): GATE_N coffees of the whole population, none labeled in
    any earlier set, stratified by platform (GATE_MIN each, the rest in proportion to each platform's size), each
    platform drawn by the lowest sha256("<split>:" + coffee_id). Each item lists the options to label: the one each
    rule's price_per_100g comes from, and, when a rule publishes none, the coffee's drawn option (to tell a rightly
    blank price from a missed one). Run only once the change's rules are frozen and committed."""
    if not split.startswith(GATE_PREFIX):
        sys.exit(f"a gate set is named {GATE_PREFIX}<change>")
    here = Path(__file__).parent
    if (here / f"items_{split}.json").exists():
        sys.exit(f"items_{split}.json exists: a gate set is drawn once")
    used = _labeled_coffees()
    pm, pc = pred(data, main), pred(data, change)
    frame: dict[str, list[dict]] = defaultdict(list)
    for r in load(data):
        if r["coffee_id"] not in used:
            frame[r["platform"]].append(r)
    sizes = {k: len(v) for k, v in frame.items()}
    total = sum(sizes.values())
    alloc = {k: min(GATE_MIN, n) for k, n in sizes.items()}
    rest = GATE_N - sum(alloc.values())
    for k, n in sizes.items():
        alloc[k] = min(n, alloc[k] + round(rest * n / total))
    drawn = []
    for k, rows in sorted(frame.items()):
        rows.sort(key=lambda r: hashlib.sha256(f"{split}:{r['coffee_id']}".encode()).hexdigest())
        for r in rows[: alloc[k]]:
            c = r["coffee_id"]
            _, vm = published(r, pm[c])
            _, vc = published(r, pc[c])
            label = [v for v in (vm, vc) if v]
            if vm is None or vc is None:
                label.append(r["item_variant_id"])
            label = list(dict.fromkeys(label))
            drawn.append({"coffee_id": c, "stratum": k, "store": r["group"], "label": label})
    (here / f"items_{split}.json").write_text(
        json.dumps(
            {
                "set": split,
                "rules": {"main": main, "change": change},
                "stratum_sizes": sizes,
                "items": sorted(drawn, key=lambda d: d["coffee_id"]),
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    n_opts = sum(len(d["label"]) for d in drawn)
    print(
        f"{split}: {len(drawn)} coffees, {n_opts} options to label; per platform {dict(Counter(d['stratum'] for d in drawn))}"
    )


CENSUS = (
    "-census"  # gate-<change>-census: the gate set's census of the coffees the change publishes differently
)


def cmd_gate_census(data: Path, split: str, main: str, change: str) -> None:
    """The gate set's census part (items_gate-<change>-census.json, ids only): every coffee of the population whose
    published price_per_100g or its option differs between main and the change, labeled fresh whatever earlier sets
    hold (the owner's choice for #139), with the same options to label as gate-sample. It is one stratum,
    "changed", of its population size; each platform stratum of the gate set then stands for its platform less its
    changed coffees (platform_sizes), so the strata add up to the whole population and nothing is counted twice.
    Refuses when the platform sample holds a changed coffee."""
    here = Path(__file__).parent
    base = split.removesuffix(CENSUS)
    if not split.endswith(CENSUS) or not (here / f"items_{base}.json").exists():
        sys.exit(f"name the census <gate>{CENSUS}, after drawing <gate>")
    if (here / f"items_{split}.json").exists():
        sys.exit(f"items_{split}.json exists: a census is drawn once")
    pm, pc = pred(data, main), pred(data, change)
    rows = load(data)
    platform = Counter(r["platform"] for r in rows)
    drawn, changed = [], Counter()
    for r in rows:
        c = r["coffee_id"]
        (xm, vm), (xc, vc) = published(r, pm[c]), published(r, pc[c])
        if (xm, vm) == (xc, vc):
            continue
        changed[r["platform"]] += 1
        label = [v for v in (vm, vc) if v]
        if vm is None or vc is None:
            label.append(r["item_variant_id"])
        drawn.append(
            {"coffee_id": c, "stratum": "changed", "store": r["group"], "label": list(dict.fromkeys(label))}
        )
    sample = {it["coffee_id"] for it in json.loads((here / f"items_{base}.json").read_text())["items"]}
    if sample & {d["coffee_id"] for d in drawn}:
        sys.exit(f"{base}'s platform sample holds changed coffees: the strata would overlap")
    (here / f"items_{split}.json").write_text(
        json.dumps(
            {
                "set": split,
                "part_of": base,
                "rules": {"main": main, "change": change},
                "stratum_sizes": {"changed": len(drawn)},
                "platform_sizes": {k: n - changed[k] for k, n in platform.items()},
                "items": sorted(drawn, key=lambda d: d["coffee_id"]),
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(
        f"{split}: {len(drawn)} coffees, {sum(len(d['label']) for d in drawn)} options to label; changed by platform {dict(changed)}"
    )


def gate_parts(gate: str) -> list[str]:
    """A gate set's parts: the platform sample, and its census when there is one."""
    here = Path(__file__).parent
    return [gate] + ([gate + CENSUS] if (here / f"items_{gate}{CENSUS}.json").exists() else [])


def gate_sizes(gate: str) -> dict[str, int]:
    """Stratum sizes for scoring a gate set: with a census, each platform less its changed coffees plus the census
    (the whole population); without one, the platform sample's frame."""
    here = Path(__file__).parent
    parts = gate_parts(gate)
    if len(parts) == 1:
        return json.loads((here / f"items_{gate}.json").read_text())["stratum_sizes"]
    census = json.loads((here / f"items_{parts[1]}.json").read_text())
    return census["platform_sizes"] | census["stratum_sizes"]


def cmd_gate_seal(data: Path, split: str, names: list[str]) -> None:
    """Each rule's published value for a gate set's coffees (pred_<name>_<set>.json: price_per_100g, the option it
    comes from and its grams), written before any label so SHA256SUMS pins them."""
    items = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
    pop = {r["coffee_id"]: r for r in load(data)}
    for name in names:
        pr = pred(data, name)
        out = {}
        for it in items:
            c = it["coffee_id"]
            x, v = published(pop[c], pr[c])
            out[c] = {"p100": x, "variant_id": v, "grams": pr[c].get(v) if v else None}
        path = Path(__file__).parent / f"pred_{name}_{split}.json"
        path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
        print(path.name, len(out))


def gate_outcome(pub: dict, labels: dict[str, int | str], drawn: str, price: float | None = None) -> str:
    """A rule's outcome on one gate coffee. Published: "right" when the option's grams are within 2% of its label,
    else "wrong" (confidently wrong; also when the option has no label: a gate never passes on what it can't
    see). Not published: "abstain_ok" when the drawn option's label gives no publishable weight (unknown, unclear,
    or outside dim_coffee's bands at that option's price), else "missed"."""
    if pub["variant_id"] is not None:
        lab = labels.get(pub["variant_id"])
        if lab is None or isinstance(lab, str):
            return "wrong"
        return "right" if abs(pub["grams"] - int(lab)) <= 0.02 * int(lab) else "wrong"
    lab = labels.get(drawn)
    if lab is None or isinstance(lab, str):
        return "abstain_ok"
    if not BAG_BAND[0] <= int(lab) <= BAG_BAND[1]:
        return "abstain_ok"
    if price and not PRICE_BAND[0] <= price / int(lab) * 100 <= PRICE_BAND[1]:
        return "abstain_ok"
    return "missed"


def gate_result(
    outcomes: dict[str, dict[str, list[tuple[str, str]]]], sizes: dict[str, int], main: str, change: str
) -> dict:
    """Weighted accuracy and confidently wrong share per rule, and change minus main, from outcomes[rule][stratum]
    = [(store, outcome)]; with normal and store-clustered 95% intervals, and the verdict."""
    out: dict = {}
    for name, per in outcomes.items():
        for label, ok in (("accuracy", ("right", "abstain_ok")), ("wrong", ("wrong",))):
            xs = {k: [float(o in ok) for _, o in v] for k, v in per.items()}
            est, lo, hi = weighted(xs, sizes)
            clo, chi = clustered({k: [(st, float(o in ok)) for st, o in v] for k, v in per.items()}, sizes)
            out[(name, label)] = (est, lo, hi, clo, chi)
    for label, ok in (("accuracy", ("right", "abstain_ok")), ("wrong", ("wrong",))):
        diff = {
            k: [
                float(b in ok) - float(m in ok)
                for (_, m), (_, b) in zip(outcomes[main][k], outcomes[change][k], strict=True)
            ]
            for k in outcomes[main]
        }
        est, lo, hi = weighted(diff, sizes)
        clo, chi = clustered(
            {
                k: [(st, x) for (st, _), x in zip(outcomes[main][k], xs, strict=True)]
                for k, xs in diff.items()
            },
            sizes,
        )
        out[("paired", label)] = (est, lo, hi, clo, chi)
    acc, rise = out[(change, "accuracy")][0], out[("paired", "wrong")][0]
    out["passes"] = acc >= GATE_ACCURACY and rise <= GATE_RISE
    return out


def gate_outcomes(rows: list[dict], grams_of) -> dict[str, list[tuple[str, str]]]:
    """Per stratum, [(store, outcome)] of one rule over gate fixture rows; grams_of(row) gives the rule's grams per
    option (a dict variant_id -> grams)."""
    per: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        listing = {"variants": [{"variant_id": o[0], "price": o[2], "available": o[4]} for o in r["options"]]}
        grams = grams_of(r)
        x, v = published(listing, grams)
        price = {o[0]: o[2] for o in r["options"]}.get(r["drawn"])
        o = gate_outcome(
            {"variant_id": v, "grams": grams.get(v) if v else None}, r["labels"], r["drawn"], price
        )
        per[r["stratum"]].append((r["store"], o))
    return per


def cmd_gate_fixture(data: Path, split: str, out: Path) -> None:
    """Append a scored gate set to the gate fixture (tests/fixtures/bag_grams_gate.jsonl): per coffee, what the rules
    read (title, description cut after its last weight line, tags, handle, every option with title, price, stored
    grams and availability), its stratum and store, the drawn option and the reference label of every labeled
    option. tests/test_bag_grams_accuracy.py scores the current rules on it."""
    from coffeedata.bag_weight import mentions

    here = Path(__file__).parent
    meta = json.loads((here / f"items_{split}.json").read_text())
    ref = json.loads((here / "labels" / f"labels_ref_{split}.json").read_text())
    pop = {r["coffee_id"]: r for r in load(data)}
    rows = [json.loads(x) for x in out.read_text().splitlines()] if out.exists() else []
    rows = [x for x in rows if x["set"] != split]
    for it in meta["items"]:
        r = pop[it["coffee_id"]]
        body = r["body_text"] or ""
        ends = [m.end for m in mentions(body)] if body else []
        body = (body if (nl := body.find("\n", max(ends))) < 0 else body[:nl]) if ends else ""
        rows.append(
            {
                "set": split,
                "coffee_id": r["coffee_id"],
                "stratum": it["stratum"],
                "store": it["store"],
                "drawn": r["item_variant_id"],
                "labels": {v: ref[f"{r['coffee_id']}|{v}"] for v in it["label"]},
                "title": r["title"],
                "body": body,
                "tags": r["tags"],
                "handle": r["handle"],
                "options": [
                    [v["variant_id"], v["title"], v["price"], v["grams"], v["available"]]
                    for v in r["variants"]
                ],
            }
        )
    out.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows))
    print(out, len(rows), "rows")


def cmd_gate_score(data: Path, gate: str, final: bool) -> None:
    """Score a gate set once (--final), with its census part when it has one: main against the change named in
    its items file, from the sealed predictions (checked against these rules' predictions) and each part's
    labels/labels_ref_<part>.json. Totals only."""
    if not final:
        sys.exit("a gate set is scored once, with --final")
    here = Path(__file__).parent
    parts = gate_parts(gate)
    metas = {part: json.loads((here / f"items_{part}.json").read_text()) for part in parts}
    main, change = metas[gate]["rules"]["main"], metas[gate]["rules"]["change"]
    pop = {r["coffee_id"]: r for r in load(data)}
    outcomes: dict[str, dict[str, list[tuple[str, str]]]] = {
        main: defaultdict(list),
        change: defaultdict(list),
    }
    for part, meta in metas.items():
        labels = json.loads((here / "labels" / f"labels_ref_{part}.json").read_text())
        for name in (main, change):
            sealed = json.loads((here / f"pred_{name}_{part}.json").read_text())
            pr = pred(data, name)
            for it in meta["items"]:
                c = it["coffee_id"]
                x, v = published(pop[c], pr[c])
                if sealed[c] != {"p100": x, "variant_id": v, "grams": pr[c].get(v) if v else None}:
                    sys.exit(f"{name}'s predictions differ from pred_{name}_{part}.json")
                labs = {o: labels[f"{c}|{o}"] for o in it["label"]}
                price = {o["variant_id"]: o["price"] for o in pop[c]["variants"]}.get(
                    pop[c]["item_variant_id"]
                )
                outcomes[name][it["stratum"]].append(
                    (it["store"], gate_outcome(sealed[c], labs, pop[c]["item_variant_id"], price))
                )
    sizes = gate_sizes(gate)
    res = gate_result(outcomes, sizes, main, change)
    print(
        f"{gate} ({' + '.join(parts)}): {sum(len(m['items']) for m in metas.values())} coffees; strata {sizes}"
    )
    for name in (main, change):
        print(f"== {name}")
        for k in sorted(outcomes[name]):
            xs = [o for _, o in outcomes[name][k]]
            cells = []
            for o in OUTCOMES:
                lo, hi = wilson(xs.count(o), len(xs))
                cells.append(f"{o} {xs.count(o)}/{len(xs)} [{100 * lo:.1f}, {100 * hi:.1f}]")
            print(f"  {k}: " + "; ".join(cells))
        for label in ("accuracy", "wrong"):
            est, lo, hi, clo, chi = res[(name, label)]
            print(
                f"  weighted {label}: {100 * est:.2f}%  normal [{100 * lo:.2f}, {100 * hi:.2f}]"
                f"  store-clustered [{100 * clo:.2f}, {100 * chi:.2f}]"
            )
    for label in ("accuracy", "wrong"):
        est, lo, hi, clo, chi = res[("paired", label)]
        print(
            f"paired {label}, {change} - {main}: {100 * est:+.2f} points  normal [{100 * lo:+.2f}, {100 * hi:+.2f}]"
            f"  store-clustered [{100 * clo:+.2f}, {100 * chi:+.2f}]"
        )
    if "changed" in outcomes[main]:
        for label, ok in (("accuracy", ("right", "abstain_ok")), ("wrong", ("wrong",))):
            m = [o in ok for _, o in outcomes[main]["changed"]]
            c = [o in ok for _, o in outcomes[change]["changed"]]
            print(f"changed stratum {label}: {main} {sum(m)}/{len(m)}, {change} {sum(c)}/{len(c)}")
    print(
        "gate:",
        "passes" if res["passes"] else "FAILS",
        f"(accuracy >= {GATE_ACCURACY}, wrong rise <= {GATE_RISE})",
    )


FIXTURE_SETS = {
    "train": "train",
    "validation": "validation",
    "test": "test-2026-11-10",
    "test2": "test-2026-11-11",
    "train-3": "train-3",
    "validation-3": "validation-3",
    "test3": "test3",
}


def cmd_fixture(data: Path, out: Path, sets: list[str]) -> None:
    """The regression fixture (CLAUDE.md step 8): each labeled item of these sets, with what the rules read (title,
    description, tags, handle, every option's title, price and stored grams), its stratum and its reference label.
    tests/test_bag_grams_accuracy.py holds the rules to floors over it. A description is cut at the end of the last
    line that mentions a weight, so the file stays small and every rule reads what it read before."""
    from coffeedata.bag_weight import mentions

    rows = []
    for split in sets:
        items = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
        labels = json.loads((Path(__file__).parent / "labels" / f"labels_ref_{split}.json").read_text())
        pop = {r["coffee_id"]: r for r in load(data, split)}
        for it in items:
            r = pop[it["coffee_id"]]
            body = r["body_text"] or ""
            ends = [m.end for m in mentions(body)] if body else []
            if ends:
                nl = body.find("\n", max(ends))
                body = body if nl < 0 else body[:nl]
            else:
                body = ""
            rows.append(
                {
                    "set": FIXTURE_SETS[split],
                    "coffee_id": r["coffee_id"],
                    "stratum": it["stratum"],
                    "label": labels[it["coffee_id"]],
                    "item": item_vid(it, r),
                    "drawn": item_vid(it, r) == r["item_variant_id"],
                    "title": r["title"],
                    "body": body,
                    "tags": r["tags"],
                    "handle": r["handle"],
                    "options": [[v["variant_id"], v["title"], v["price"], v["grams"]] for v in r["variants"]],
                }
            )
    out.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows))
    print(out, len(rows), out.stat().st_size // 1024, "KB")


def cmd_seal(data: Path, split: str, names: list[str]) -> None:
    """Each rule's grams for the set's labeled options, written here (pred_<name>_<set>.json: ids and grams only)
    before any label exists, so SHA256SUMS can pin them."""
    items = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
    pop = {r["coffee_id"]: r for r in load(data, split)}
    for name in names:
        pr = pred(data, name)
        out = {it["coffee_id"]: pr[it["coffee_id"]].get(item_vid(it, pop[it["coffee_id"]])) for it in items}
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
        if "label_from" in it:
            continue  # labeled in an earlier round, for this same option
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
            }
            | (
                {"label_these_options": it["label"]}
                if "label" in it
                else {"label_this_option": item_vid(it, r)}
            )
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


def label_keys(it: dict) -> list[str]:
    """The keys a labeler answers for an item: its coffee_id, or for a gate set (several options per coffee)
    "<coffee_id>|<variant_id>" per option to label."""
    if "label" in it:
        return [f"{it['coffee_id']}|{v}" for v in it["label"]]
    return [it["coffee_id"]]


def cmd_agree(split: str, a: Path, b: Path) -> None:
    """Agreement of two independent labelers (totals), and the items a third must settle (to_settle_<set>.json,
    ids only, written next to the labels)."""
    la, lb = json.loads(a.read_text()), json.loads(b.read_text())
    items = [
        k
        for it in json.loads((Path(__file__).parent / f"items_{split}.json").read_text())["items"]
        if "label_from" not in it
        for k in label_keys(it)
    ]
    missing = [i for i in items if i not in la or i not in lb]
    stray = sorted((set(la) | set(lb)) - set(items))
    if missing or stray:
        print(
            f"{len(missing)} items lack a label from A or B (C settles them); {len(stray)} keys name no item"
        )
    diff = sorted(i for i in items if i in missing or not same(la[i], lb[i]))
    lo, hi = wilson(len(items) - len(diff), len(items))
    print(
        f"{split}: agree {len(items) - len(diff)}/{len(items)} [{100 * lo:.1f}, {100 * hi:.1f}]; to settle {len(diff)}"
    )
    (a.parent / f"to_settle_{split}.json").write_text(json.dumps(diff, indent=1) + "\n")


def cmd_settle(split: str, a: Path, b: Path, c: Path, out: Path) -> None:
    """The reference, item by item: A where A and B agree, else C (the tiebreak labeled only the disagreements, and
    any item A or B left without a label, e.g. under a mistyped key). Keys that name no item are ignored."""
    la, lb, lc = (json.loads(p.read_text()) for p in (a, b, c))
    ref = {}
    here = Path(__file__).parent
    earlier: dict[str, dict] = {}
    for it in json.loads((here / f"items_{split}.json").read_text())["items"]:
        if "label_from" in it:  # an earlier round's reference label for the same option
            i, src = it["coffee_id"], it["label_from"]
            if src not in earlier:
                earlier[src] = json.loads((here / "labels" / f"labels_ref_{src}.json").read_text())
            ref[i] = earlier[src][i]
            continue
        for i in label_keys(it):
            if i in la and i in lb and same(la[i], lb[i]):
                ref[i] = la[i]
            elif i in lc:
                ref[i] = lc[i]
            else:
                sys.exit(f"{i}: A and B disagree or one has no label, and C has no label")
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


def clustered(
    per: dict[str, list[tuple[str, float]]], sizes: dict[str, int], reps: int = 2000
) -> tuple[float, float]:
    """A 95% interval for the stratum-weighted mean that treats a store's items as one draw: within each stratum the
    sampled stores are resampled with replacement (a store-clustered, stratified bootstrap; fixed seed)."""
    rng = random.Random(f"{SALT}:bootstrap")
    total = sum(sizes[k] for k in per)
    by_store = {k: defaultdict(list) for k in per}
    for k, xs in per.items():
        for store, x in xs:
            by_store[k][store].append(x)
    stores = {k: list(v.values()) for k, v in by_store.items()}
    ests = []
    for _ in range(reps):
        est = 0.0
        for k, groups in stores.items():
            if not groups:
                continue
            pick = [x for _ in groups for x in rng.choice(groups)]
            est += sizes[k] / total * sum(pick) / len(pick)
        ests.append(est)
    ests.sort()
    return ests[int(0.025 * reps)], ests[int(0.975 * reps) - 1]


def weighted(per: dict[str, list[float]], sizes: dict[str, int]) -> tuple[float, float, float]:
    """A stratum-weighted mean of per-item scores, with a normal-approximation 95% interval over the strata's
    variances (items taken as independent; clustered() is the store-clustered one)."""
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
    intervals; the stratum-weighted accuracy (right + abstain_ok), wrong share and covered-right share, each with a
    normal-approximation 95% interval and a store-clustered bootstrap one; and the paired difference branch - main
    on the same items, with both intervals. Items labeled "unclear" are left out and counted."""
    if split in TEST_SETS and not final:
        sys.exit("the test set is scored once, with --final")
    meta = json.loads((Path(__file__).parent / f"items_{split}.json").read_text())
    labels = json.loads(labels_file.read_text())
    sizes, items = meta["stratum_sizes"], meta["items"]
    pop = {r["coffee_id"]: r for r in load(data, split)}
    store = {it["coffee_id"]: it.get("store") or pop[it["coffee_id"]]["group"] for it in items}
    vid = {it["coffee_id"]: item_vid(it, pop[it["coffee_id"]]) for it in items}
    preds = {name: pred(data, name) for name in (main, branch)}
    for name, pr in preds.items():  # the sealed predictions must be the ones scored
        sealed = Path(__file__).parent / f"pred_{name}_{split}.json"
        if sealed.exists():
            want = json.loads(sealed.read_text())
            got = {i: pr[i].get(vid[i]) for i in want}
            if got != want:
                sys.exit(f"{name}'s predictions differ from {sealed.name}")
        elif (
            split in TEST_SETS
        ):  # review L4: a test set is scored only against rules sealed before its labels
            sys.exit(f"{sealed.name} is missing: seal {name} before scoring {split}")
    res: dict[str, dict[str, list[str]]] = {name: defaultdict(list) for name in preds}
    stores: dict[str, list[str]] = defaultdict(list)
    unclear: Counter = Counter()
    for it in items:
        lab = labels.get(it["coffee_id"])
        if lab is None or lab == "unclear":
            unclear[it["stratum"]] += 1
            continue
        stores[it["stratum"]].append(store[it["coffee_id"]])
        for name, pr in preds.items():
            g = pr[it["coffee_id"]].get(vid[it["coffee_id"]])
            res[name][it["stratum"]].append(outcome(g, lab))
    print(
        f"{split}: {len(items)} items, unclear {sum(unclear.values())} {dict(unclear)}; stratum sizes {sizes}"
    )
    for k in sorted(stores):
        top = Counter(stores[k]).most_common(1)[0]
        print(f"  {k}: {len(stores[k])} items from {len(set(stores[k]))} stores; largest {top[1]} ({top[0]})")
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
            clo, chi = clustered(
                {
                    k: [(st, float(o in ok)) for st, o in zip(stores[k], xs, strict=True)]
                    for k, xs in res[name].items()
                },
                sizes,
            )
            print(
                f"  weighted {label}: {100 * est:.1f}%  normal [{100 * lo:.1f}, {100 * hi:.1f}]"
                f"  store-clustered [{100 * clo:.1f}, {100 * chi:.1f}]"
            )
    for label, ok in (("accuracy", ("right", "abstain_ok")), ("wrong", ("wrong",))):
        diff = {
            k: [float(b in ok) - float(m in ok) for m, b in zip(res[main][k], res[branch][k], strict=True)]
            for k in res[main]
        }
        est, lo, hi = weighted(diff, sizes)
        clo, chi = clustered({k: list(zip(stores[k], xs, strict=True)) for k, xs in diff.items()}, sizes)
        print(
            f"paired {label}, {branch} - {main}: {100 * est:+.1f} points  normal [{100 * lo:+.1f}, {100 * hi:+.1f}]"
            f"  store-clustered [{100 * clo:+.1f}, {100 * chi:+.1f}]"
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
    elif cmd == "changed":
        cmd_changed(data, a[2], a[3], a[4:])
    elif cmd == "gate-d":
        cmd_gate_d(data, a[2], a[3], Path(a[4]))
    elif cmd == "gate":
        cmd_gate(data, a[2:])
    elif cmd == "gate-sample":
        cmd_gate_sample(data, a[2], a[3], a[4])
    elif cmd == "gate-seal":
        cmd_gate_seal(data, a[2], a[3:])
    elif cmd == "gate-fixture":
        cmd_gate_fixture(data, a[2], Path(a[3]))
    elif cmd == "gate-census":
        cmd_gate_census(data, a[2], a[3], a[4])
    elif cmd == "gate-score":
        cmd_gate_score(data, a[2], "--final" in a)
    elif cmd == "fixture":
        cmd_fixture(data, Path(a[2]), a[3:])
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
