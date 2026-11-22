"""Provisional evaluation of the roaster screen's rules (README.md). Train and validation only.

    python screen_eval.py features <evidence-dir> <set>            rescore #130's stored evidence, first page only
    python screen_eval.py show     <evidence-dir> train [tier]     train only: each item's features, tier and label
    python screen_eval.py score    <evidence-dir> <set> [rule...]  aggregates per candidate rule, Wilson 95% intervals
    python screen_eval.py agree                                    the two settles compared, counts only

There is no `final`: no test set is drawn, and none is scored here. Nothing prints an item of the validation set.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from coffeedata import screen_rules as sr

D = Path(__file__).resolve().parent
ROOT = D.parents[1]
SETS = ("train", "validation")
WRONG_KINDS = ("cafe", "reseller")
# The first page the new screen reads (screen.PAGE_SIZES): #130 read up to 8 pages, so its evidence is cut to this.
FIRST_PAGE = {"shopify": 250, "woocommerce": 25}

# The candidate rule sets of the design's section 4.2, each at N in 2, 3 and 5.
BASES = {
    "full": sr.Rule(name="full"),
    "no_depth": sr.Rule(name="no_depth", depth=False),
    "no_own_vendor": sr.Rule(name="no_own_vendor", own_vendor_path=False),
    "own_brand": sr.Rule(name="own_brand", coffee_share=0.0, bag_share=0.0, depth=False),
}
CANDIDATES = {
    f"{k},N={n}": replace(r, name=f"{k},N={n}", min_named=n) for k, r in BASES.items() for n in (2, 3, 5)
}


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def pick_labellers(preference: list[str], decider: str | None = None) -> tuple[str, str, str]:
    """The Claude stage's reference labelers (README, "The Claude stage's evaluation"): from the models available at
    draw time, in their order of preference, the first three distinct ones that aren't the model whose decisions are
    scored (screen_verdict.model() unless `decider` is given): A and B label, C settles. Fewer than three left is an
    error, never a reference made by the model under test."""
    from coffeedata import screen_verdict

    decider = decider or screen_verdict.model()
    left = list(dict.fromkeys(m for m in preference if m != decider))
    if len(left) < 3:
        raise SystemExit(f"need three models other than the decider ({decider}); got {left}")
    return left[0], left[1], left[2]


def known() -> sr.KnownRoasters:
    with (ROOT / "config" / "roasters.csv").open(newline="") as f:
        return sr.KnownRoasters(r["name"] for r in csv.DictReader(f))


def truth(set_name: str) -> tuple[dict[str, dict], str]:
    """The reference labels: where A and B agree, and the third labeler's settles for the rest. settled_<set>.json
    (the labeler README.md names) when it exists; until then #130's third labeler's, which is PROVISIONAL."""
    t = dict(json.loads((D / f"agreed_{set_name}.json").read_text()))
    new = D / f"settled_{set_name}.json"
    source = "settled" if new.exists() else "settled_C (PROVISIONAL)"
    for r in json.loads((new if new.exists() else D / f"settled_C_{set_name}.json").read_text()):
        t[r["id"]] = r
    return {k: v for k, v in t.items() if v["label"] in ("in_scope", "out_of_scope")}, source


def load_features(data: Path, set_name: str) -> dict[str, dict]:
    """Each sampled item's features under the current code, from #130's evidence cut to the first page."""
    items = {i["domain"]: i for i in json.loads((D / f"items_{set_name}.json").read_text())}
    kn = known()
    out = {}
    # #130's train and validation evidence, never its test's: an item that moved between the two (README, Split)
    # keeps the evidence #130 stored under its old set
    lines = []
    for old in ("train", "validation"):
        with gzip.open(data / f"evidence_{old}.jsonl.gz", "rt") as f:
            lines += f.readlines()
    for line in lines:
        e = json.loads(line)
        it = items.get(e["roaster_id"])
        if it is None:
            continue
        res = e["result"]
        platform = res.get("platform") or ""
        size = FIRST_PAGE.get(platform)
        # status as the new screen records it: #130's own budget and catalog errors happened after an ok probe
        status = res.get("status") or "unreachable"
        status = "ok" if status in ("budget_spent", "error") and platform else status
        raw = (e.get("items") or [])[:size] if size and status == "ok" else []
        rows = [
            sr.product_signals(
                p | {"variants": [v | {"price": v.get("price_usd")} for v in p.get("variants") or []]},
                it["name"], it["domain"], kn,
            )
            for p in raw
        ]  # fmt: skip
        screen = {"status": status, "refusal": "robots_disallow" if status == "blocked" else None,
                  "platform": platform, "page_full": bool(size and len(e.get("items") or []) >= size),
                  "page_timeout": False}  # fmt: skip
        home = sr.home_signals(e.get("home_text") if status == "ok" else None, it["name"], kn)
        out[it["id"]] = sr.features(screen, rows, home)
    return out


def metrics(set_name: str, feats: dict[str, dict], rule: sr.Rule) -> dict:
    """Stratum-weighted, on #130's strata (sample_<set>.json: tag x whether #130's screen found coffee); Wilson 95%
    intervals on the labeled counts (an approximation for a weighted ratio, as #130 stated)."""
    sample = json.loads((D / f"sample_{set_name}.json").read_text())
    t, source = truth(set_name)
    w: Counter = Counter()
    n: Counter = Counter()
    for key, st in sample["strata"].items():
        ids = [i for i, s in sample["stratum_of"].items() if s == key and i in t and i in feats]
        if not ids:
            continue
        each = st["population"] / len(ids)
        for i in ids:
            tr = sr.tier(feats[i], rule)[0]
            pos = t[i]["label"] == "in_scope"
            flags = {
                "all": True, "pos": pos, tr: True, f"{tr}_pos": pos,
                "suggested_wrong": tr == "suggested" and not pos and t[i].get("kind") in WRONG_KINDS,
                "found_pos": pos and tr in ("suggested", "review"),
            }  # fmt: skip
            for k, v in flags.items():
                w[k] += each * v
                n[k] += v
    return {"w": w, "n": n, "source": source, "labeled": len([i for i in t if i in feats])}


def pct(wk: float, wn: float, nk: int, nn: int) -> str:
    if not nn:
        return "n/a (0)"
    lo, hi = wilson(nk, nn)
    return f"{wk / wn:.1%} [{lo:.1%}, {hi:.1%}] ({nk}/{nn})"


def score(data: Path, set_name: str, names: list[str]) -> None:
    if unknown := [n for n in names if n not in CANDIDATES]:
        sys.exit(f"no such rule: {', '.join(unknown)}. One of: {', '.join(CANDIDATES)}")
    feats = load_features(data, set_name)
    print(f"| Rule | Labeled | Suggested | Precision of suggested | Suggested a café or reseller | Recall, suggested | "
          f"Recall, suggested + review | Dropped in scope | Review per 1,000 |\n|{'---|' * 9}")  # fmt: skip
    for name in names or list(CANDIDATES):
        m = metrics(set_name, feats, CANDIDATES[name])
        w, n = m["w"], m["n"]
        print(
            f"| {name} | {m['labeled']} | {n['suggested']} | "
            f"{pct(w['suggested_pos'], w['suggested'], n['suggested_pos'], n['suggested'])} | "
            f"{pct(w['suggested_wrong'], w['suggested'], n['suggested_wrong'], n['suggested'])} | "
            f"{pct(w['suggested_pos'], w['pos'], n['suggested_pos'], n['pos'])} | "
            f"{pct(w['found_pos'], w['pos'], n['found_pos'], n['pos'])} | "
            f"{pct(w['dropped_pos'], w['dropped'], n['dropped_pos'], n['dropped'])} | "
            f"{1000 * w['review'] / w['all']:.0f} |"
        )
    print(f"\n{set_name}: reference labels = agreed + {m['source']}; unsure left out; main suggests nothing.")
    tiers = Counter(sr.tier(f)[0] for f in feats.values())
    print(
        f"tiers under {sr.RULE_VERSION} (unweighted, all {len(feats)} sampled items): {dict(sorted(tiers.items()))}"
    )


def show(data: Path, set_name: str, only: str | None) -> None:
    if set_name != "train":
        sys.exit("only the train set's examples are read")
    feats = load_features(data, "train")
    t, _ = truth("train")
    items = {i["id"]: i for i in json.loads((D / "items_train.json").read_text())}
    for i, f in sorted(feats.items()):
        tr, why = sr.tier(f)
        if only and tr != only:
            continue
        lab = t.get(i, {"label": "unsure", "kind": "?"})
        print(f"{tr:12} {lab['label']:12} {lab.get('kind', ''):8} {items[i]['domain']:34} {f['platform']:12} "
              f"p={f['products']} n={f['named']} cs={f['coffee_share']:.2f} bs={f['bag_share']:.2f} "
              f"rs={f['roast_share']:.2f} or={f['origins']} ov={f['own_vendor_share']:.2f} "
              f"vend={f['vendors']} oth={f['other_roaster']} ws={f['wholesale_share']:.2f} "
              f"roast={f['roast_phrase']!r} serves={f['serves_phrase']!r} | {'; '.join(why)}")  # fmt: skip


def agree() -> None:
    """How often #130's third labeler and the new one settled an item the same way (label and kind), per set."""
    for s in SETS:
        new = D / f"settled_{s}.json"
        if not new.exists():
            print(f"{s}: settled_{s}.json isn't there yet")
            continue
        a = {r["id"]: r for r in json.loads(new.read_text())}
        c = {r["id"]: r for r in json.loads((D / f"settled_C_{s}.json").read_text())}
        same = sum((a[i]["label"], a[i]["kind"]) == (c[i]["label"], c[i]["kind"]) for i in a)
        label = sum(a[i]["label"] == c[i]["label"] for i in a)
        lo, hi = wilson(same, len(a))
        print(f"{s}: {same}/{len(a)} the same label and kind = {same / len(a):.1%} [{lo:.1%}, {hi:.1%}]; "
              f"{label}/{len(a)} the same label")  # fmt: skip


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "score" and len(args) >= 2 and args[1] in SETS:
        score(Path(args[0]), args[1], args[2:])
    elif cmd == "show" and len(args) >= 2:
        show(Path(args[0]), args[1], args[2] if len(args) > 2 else None)
    elif cmd == "agree":
        agree()
    else:
        sys.exit(__doc__)
