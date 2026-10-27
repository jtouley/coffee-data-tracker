"""Sealed test set. Prints aggregates only, never a product.

  python score_sealed.py agree                 agreement between the two labels of each product; writes the
                                               disagreements (products and the two labels, no predictions) for
                                               a third labeler to settle
  python score_sealed.py final <predictions>   one score of a frozen classifier: predictions is a JSON
                                               {id: true|false}; prints stratum-weighted accuracy and both error
                                               rates with 95% Wilson intervals
"""

import json
import math
import sys
from pathlib import Path

D = Path(__file__).parent
sample = json.loads((D / "sample.json").read_text())
items = {d["id"]: d for d in sample["items"]}


def labels(name):
    return {r["id"]: r["label"] for r in json.loads((D / name).read_text())}


def wilson(k, n, z=1.96):
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, mid - half), min(1.0, mid + half)


if sys.argv[1] == "agree":
    a, b = {**labels("labels_A.json"), **labels("labels_C.json")}, {**labels("labels_B.json"), **labels("labels_D.json")}
    same = [i for i in a if a[i] == b[i] and a[i] != "unsure"]
    print(f"agreement {len(same)}/{len(a)} = {len(same) / len(a):.1%}")
    dis = [{k: items[i][k] for k in ("id", "roaster_name", "title", "product_type", "tags", "grams", "body")}
           | {"label_1": a[i], "label_2": b[i]} for i in a if i not in same]
    (D / "to_settle.json").write_text(json.dumps(dis, ensure_ascii=False, default=str, indent=0))
    final = {i: a[i] for i in same}
    (D / "agreed.json").write_text(json.dumps(final))
    print(f"to settle: {len(dis)}")
    sys.exit()

if sys.argv[1] == "final":
    truth = {int(k): v for k, v in json.loads((D / "agreed.json").read_text()).items()}
    truth |= {r["id"]: r["label"] for r in json.loads((D / "settled.json").read_text())}
    pred = {int(k): v for k, v in json.loads(Path(sys.argv[2]).read_text()).items()}
    pop = sample["population"]
    total_err = 0.0
    for stratum, key in (("kept", "kept"), ("dropped", "dropped")):
        ids = [i for i, d in items.items() if d["stratum"] == stratum and truth.get(i) in ("coffee", "not_coffee")]
        fp = sum(pred[i] and truth[i] == "not_coffee" for i in ids)
        fn = sum((not pred[i]) and truth[i] == "coffee" for i in ids)
        e, lo, hi = wilson(fp + fn, len(ids))
        total_err += e * pop[key]
        print(f"{stratum:8} n={len(ids)} kept-but-not-coffee={fp} dropped-but-coffee={fn} "
              f"error {e:.1%} (95% CI {lo:.1%}-{hi:.1%})")
    N = pop["kept"] + pop["dropped"]
    print(f"weighted accuracy over {N:,} products: {1 - total_err / N:.2%}")
