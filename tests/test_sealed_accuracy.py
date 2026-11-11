"""The classifier's sealed set, now a regression fixture (eval/sealed/README.md, CLAUDE.md step 5).

It was scored once, on 2026-10-28 (eval/sealed/RESULTS.md), so it joins the floors. Two floors, both stratum-weighted
as score_sealed.py weights them, over the 592 products the labelers didn't mark unsure:
- what the build decides (products.decide, stg_products) from Claude's frozen verdicts on the set
  (pred_claude-haiku-5-5.json, the model in production): a verdict counts unless the rules exclude the product;
- the rules alone (classify.is_coffee), which decide wherever there is no verdict.
Only totals are printed.
"""

import json
from pathlib import Path

import pytest

from coffeedata import classify, products

SEALED = Path(__file__).parent.parent / "eval" / "sealed"
# Measured 2026-11-11 on main 0cfa4d9: decide 0.9827 (kept 8 false coffee, dropped 3 missed), the rules 0.9405
# (kept 32 / dropped 2 false coffee and 5 missed). Raise these when the classifier improves, never lower them.
FLOOR = {"decide": 0.982, "rules": 0.940}


def _weighted_accuracy(pred: dict[int, bool]) -> float:
    sample = json.loads((SEALED / "sample.json").read_text())
    truth = {int(k): v for k, v in json.loads((SEALED / "agreed.json").read_text()).items()}
    truth |= {r["id"]: r["label"] for r in json.loads((SEALED / "settled.json").read_text())}
    pop, err = sample["population"], 0.0
    for stratum in ("kept", "dropped"):
        ids = [
            d["id"]
            for d in sample["items"]
            if d["stratum"] == stratum and truth.get(d["id"]) in ("coffee", "not_coffee")
        ]
        wrong = sum(pred[i] != (truth[i] == "coffee") for i in ids)
        err += wrong / len(ids) * pop[stratum]
    return 1 - err / (pop["kept"] + pop["dropped"])


def _predictions(which: str) -> dict[int, bool]:
    verdicts = {int(k): v for k, v in json.loads((SEALED / "pred_claude-haiku-5-5.json").read_text()).items()}
    out = {}
    for d in json.loads((SEALED / "inputs.json").read_text()):
        it = products.item(d["title"], d["product_type"], d["tags"], d["grams"], "", d["body_text"])
        if which == "rules":
            out[d["id"]] = classify.is_coffee(it)
        else:
            out[d["id"]] = products.decide(it, "coffee" if verdicts[d["id"]] else "not_coffee")[0]
    return out


@pytest.mark.parametrize("which", sorted(FLOOR))
def test_sealed_set_accuracy_does_not_regress(which):
    acc = _weighted_accuracy(_predictions(which))
    assert acc >= FLOOR[which], f"{which}: weighted accuracy {acc:.2%} < {FLOOR[which]:.1%}"
