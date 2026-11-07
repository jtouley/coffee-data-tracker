"""Score the guard against main on one set, from the reference labels (labels_ref.json).

    python score.py <EVAL_DIR> settle labels_A.json labels_B.json labels_C.json
        writes labels_ref.json: A where A and B agree, C where they don't, and prints the agreement only.
    python score.py <EVAL_DIR> <train|validation|test>
        prints totals only: each arm's accuracy per stratum (Wilson 95%) and weighted by stratum size, and each
        arm's confidently wrong answers (a product kept public that isn't coffee, or removed that is).

A labels file is {coffee_id: coffee | not_coffee | unsure}, made blind from work/items_<set>.json against GUIDE.md.
Unsure labels are left out of accuracy and counted. Strata sizes come from work/population.json (pinned by
SHA256SUMS). Run the test set once, after the guard is frozen (README.md).
"""

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).parent
ARMS = ("main", "guard")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def settle(a: str, b: str, c: str) -> None:
    la, lb, lc = (json.loads((HERE / f).read_text()) for f in (a, b, c))
    ids = sorted(set(la) & set(lb))
    agree = sum(la[i] == lb[i] for i in ids)
    missing = [i for i in ids if la[i] != lb[i] and i not in lc]
    if missing:
        raise SystemExit(f"{len(missing)} disagreements have no tiebreak label")
    ref = {i: la[i] if la[i] == lb[i] else lc[i] for i in ids}
    (HERE / "labels_ref.json").write_text(json.dumps(ref, sort_keys=True, indent=0))
    lo, hi = wilson(agree, len(ids))
    print(
        f"agreement {agree}/{len(ids)} = {agree / len(ids):.1%} [{lo:.1%}, {hi:.1%}]; {len(ids) - agree} settled"
    )


def score(eval_dir: str, name: str) -> None:
    pop = [
        r for r in json.loads((Path(eval_dir) / "work" / "population.json").read_text()) if r["set"] == name
    ]
    size = {s: sum(r["stratum"] == s for r in pop) for s in ("D", "N")}
    stratum = {r["coffee_id"]: r["stratum"] for r in pop}
    ids = (HERE / f"samples_{name}.txt").read_text().split()
    ref = json.loads((HERE / "labels_ref.json").read_text())
    preds = {a: json.loads((HERE / f"preds_{a}.json").read_text()) for a in ARMS}
    labeled = [i for i in ids if i in ref]
    unsure = sum(ref[i] == "unsure" for i in labeled)
    print(f"{name}: {len(labeled)} of {len(ids)} labeled, {unsure} unsure (left out); population D {size['D']}, "
          f"N {size['N']}")  # fmt: skip
    for a in ARMS:
        parts, total_w = [], 0.0
        for s in ("D", "N"):
            xs = [i for i in labeled if stratum[i] == s and ref[i] != "unsure"]
            k = sum(preds[a][i] == ref[i] for i in xs)
            lo, hi = wilson(k, len(xs))
            parts.append(f"{s} {k}/{len(xs)} [{lo:.1%}, {hi:.1%}]")
            total_w += size[s] / sum(size.values()) * (k / len(xs) if xs else 0)
        kept_wrong = sum(preds[a][i] == "coffee" and ref[i] == "not_coffee" for i in labeled)
        removed_wrong = sum(preds[a][i] == "not_coffee" and ref[i] == "coffee" for i in labeled)
        print(f"  {a}: weighted accuracy {total_w:.1%}; {'; '.join(parts)}; confidently wrong: "
              f"{kept_wrong} kept and not coffee, {removed_wrong} removed and coffee")  # fmt: skip


if __name__ == "__main__":
    if sys.argv[2] == "settle":
        settle(*sys.argv[3:6])
    else:
        score(sys.argv[1], sys.argv[2])
