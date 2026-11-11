"""coffeedata.bag_weight against labeled listings (CLAUDE.md step 8): two independent labels each, made by Claude
subagents against eval/bag_grams/GUIDE.md, with disagreements settled by a third.

tests/fixtures/bag_grams_labels.jsonl holds the labeled items of eval/bag_grams: "train" (the rules were tuned on
it), "validation" (versions were chosen on its totals), "test-2026-11-10" (the first sealed test set) and "test-2026-11-11"
(the second, test2), each scored once and then added here as a regression fixture. Each item is one option of a current retail listing, drawn by stratum;
the scores weight each stratum by its size in the set (eval/bag_grams/items_<set>.json). A rules change must not
lower any accuracy or raise any confidently wrong share.
"""

import json
from collections import defaultdict
from pathlib import Path

import pytest

from coffeedata.bag_weight import product_grams

ROOT = Path(__file__).parent.parent
LABELS = Path(__file__).parent / "fixtures" / "bag_grams_labels.jsonl"
ITEMS = {"train": "train", "validation": "validation", "test-2026-11-10": "test", "test-2026-11-11": "test2"}
# Measured on 2026-11-11 with the rules frozen for the second test set; raise these when the rules improve, never
# lower them to pass. Accuracy: the share right (grams within 2% of the label) or rightly blank (label "unknown").
# Wrong: the share confidently wrong (a weight for an "unknown" label, or off by more than 2%).
FLOOR = {"train": 0.999, "validation": 0.956, "test-2026-11-10": 0.926, "test-2026-11-11": 0.953}
CEILING = {"train": 0.001, "validation": 0.011, "test-2026-11-10": 0.0161, "test-2026-11-11": 0.0087}


def _scores(name: str) -> tuple[float, float, list[str]]:
    sizes = json.loads((ROOT / "eval" / "bag_grams" / f"items_{ITEMS[name]}.json").read_text())[
        "stratum_sizes"
    ]
    per: dict[str, list[str]] = defaultdict(list)
    shown = []
    for r in map(json.loads, LABELS.read_text().splitlines()):
        if r["set"] != name or r["label"] == "unclear":
            continue
        opts = [(o[1], o[2], o[3]) for o in r["options"]]
        grams = dict(
            zip(
                [o[0] for o in r["options"]],
                product_grams(r["title"], r["body"], opts, r["tags"], r["handle"]),
                strict=True,
            )
        )
        g, label = grams[r["item"]], r["label"]
        if g is None:
            outcome = "ok" if label == "unknown" else "missed"
        elif label == "unknown" or abs(g - int(label)) > 0.02 * int(label):
            outcome = "wrong"
        else:
            outcome = "ok"
        per[r["stratum"]].append(outcome)
        if outcome != "ok":
            shown.append(f"  {outcome:6} {label!s:>7} got {g!s:>6}  {r['title'][:70]}")
    total = sum(sizes[k] for k in per)
    acc = sum(sizes[k] / total * xs.count("ok") / len(xs) for k, xs in per.items())
    wrong = sum(sizes[k] / total * xs.count("wrong") / len(xs) for k, xs in per.items())
    return acc, wrong, shown


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_accuracy_does_not_regress(name):
    acc, wrong, shown = _scores(name)
    # Only train's misses are shown: validation is judged on its totals, never example by example.
    misses = "\n".join(shown) if name == "train" else ""
    assert acc >= FLOOR[name], f"{name}: weighted accuracy {acc:.4f} < floor {FLOOR[name]}\n{misses}"
    assert wrong <= CEILING[name], f"{name}: weighted wrong {wrong:.4f} > ceiling {CEILING[name]}\n{misses}"
