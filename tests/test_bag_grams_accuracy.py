"""coffeedata.bag_weight against labeled listings (CLAUDE.md step 8): two independent labels each, made by Claude
subagents against eval/bag_grams/GUIDE.md, with disagreements settled by a third.

tests/fixtures/bag_grams_labels.jsonl holds the labeled items of eval/bag_grams: "train" (the rules were tuned on
it), "validation" (versions were chosen on its totals), "test-2026-11-10" (the first sealed test set) and "test-2026-11-11"
(the second, test2), each scored once and then added here as a regression fixture; and round 3's "train-3" and
"validation-3" (every coffee the bag-count change touches in those splits) and its sealed "test3", scored once. Each item is one option of a current retail listing, drawn by stratum;
the scores weight each stratum by its size in the set (eval/bag_grams/items_<set>.json). A rules change must not
lower any accuracy or raise any confidently wrong share.
"""

import hashlib
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

import pytest

from coffeedata.bag_weight import product_grams

ROOT = Path(__file__).parent.parent
LABELS = Path(__file__).parent / "fixtures" / "bag_grams_labels.jsonl"
ITEMS = {
    "train": "train",
    "validation": "validation",
    "test-2026-11-10": "test",
    "test-2026-11-11": "test2",
    "train-3": "train-3",
    "validation-3": "validation-3",
    "test3": "test3",
}
# Measured on 2026-11-11 with the rules frozen for round 3 (bag counts); raise these when the rules improve, never
# lower them to pass. Accuracy: the share right (grams within 2% of the label) or rightly blank (label "unknown").
# Wrong: the share confidently wrong (a weight for an "unknown" label, or off by more than 2%). train-3 and
# validation-3 are round 3's census of the coffees that change (eval/bag_grams/README.md): low on train-3 because
# its misses are listings the rules leave blank (the label is a weight), not wrong ones.
FLOOR = {
    "train": 0.999,
    "validation": 0.958,
    "test-2026-11-10": 0.933,
    "test-2026-11-11": 0.953,
    "train-3": 0.717,
    "validation-3": 0.916,
    "test3": 0.819,
}
CEILING = {
    "train": 0.001,
    "validation": 0.0106,
    "test-2026-11-10": 0.0088,
    "test-2026-11-11": 0.0075,
    "train-3": 0.0257,
    "validation-3": 0.0834,
    "test3": 0.0,
}


def _scores(name: str) -> tuple[float, float, list[str]]:
    sizes = json.loads((ROOT / "eval" / "bag_grams" / f"items_{ITEMS[name]}.json").read_text())[
        "stratum_sizes"
    ]
    per: dict[str, list[str]] = defaultdict(list)
    shown = []
    for r in map(json.loads, LABELS.read_text().splitlines()):
        if r["set"] != name or r["label"] == "unclear":
            continue
        g, label = _grams(r), r["label"]
        outcome = _outcome(g, label)
        per[r["stratum"]].append(outcome)
        if outcome != "ok":
            shown.append(f"  {outcome:6} {label!s:>7} got {g!s:>6}  {r['title'][:70]}")
    total = sum(sizes[k] for k in per)
    acc = sum(sizes[k] / total * xs.count("ok") / len(xs) for k, xs in per.items())
    wrong = sum(sizes[k] / total * xs.count("wrong") / len(xs) for k, xs in per.items())
    return acc, wrong, shown


def _grams(r: dict) -> int | None:
    """The current rules' grams for the row's labeled option."""
    opts = [(o[1], o[2], o[3]) for o in r["options"]]
    grams = product_grams(r["title"], r["body"], opts, r["tags"], r["handle"])
    return dict(zip([o[0] for o in r["options"]], grams, strict=True))[r["item"]]


def _outcome(g: int | None, label: int | str) -> str:
    if g is None:
        return "ok" if label == "unknown" else "missed"
    if label == "unknown" or abs(g - int(label)) > 0.02 * int(label):
        return "wrong"
    return "ok"


# The full-population gate (CLAUDE.md step 7; eval/bag_grams/README.md "The full-population gate"). Each rules change
# draws a fresh, sealed gate set: coffees of the whole population, stratified by platform, disjoint from every earlier
# set, labeled for the option each rule's published price_per_100g comes from. tests/fixtures/bag_grams_gate.jsonl
# holds the scored gate sets; main's published values at the time are the sealed pred_<main>_<set>.json. On every
# gate set the current rules must reach bag_eval.GATE_ACCURACY, weighted by platform size, with their confidently
# wrong share at most bag_eval.GATE_RISE above that main's. bag_eval is loaded by path (nothing goes on sys.path).
_spec = importlib.util.spec_from_file_location("bag_grams_eval", ROOT / "eval" / "bag_grams" / "bag_eval.py")
assert _spec and _spec.loader
bag_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bag_eval)
GATE = Path(__file__).parent / "fixtures" / "bag_grams_gate.jsonl"
EVAL = ROOT / "eval" / "bag_grams"


def _gate_sets() -> dict[str, list[dict]]:
    rows = [json.loads(x) for x in GATE.read_text().splitlines()] if GATE.exists() else []
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:  # a gate set's census part is scored with it
        out[r["set"].removesuffix(bag_eval.CENSUS)].append(r)
    return dict(out)


def _rules_grams(r: dict, damage: str | None = None) -> dict:
    """The current rules' grams per option of a gate row; a mutation check damages them: "double" doubles every
    weight, "blank" leaves the coffee without any."""
    opts = [(o[1], o[2], o[3]) for o in r["options"]]
    grams = product_grams(r["title"], r["body"], opts, r["tags"], r["handle"])
    if damage == "double":
        grams = [g * 2 if g else g for g in grams]
    elif damage == "blank":
        grams = [None for _ in grams]
    return dict(zip([o[0] for o in r["options"]], grams, strict=True))


def _gate(name: str, rows: list[dict], damage: dict[str, str] | None = None) -> dict:
    """The gate on one gate set: main's sealed published values against the current rules, with `damage`
    (coffee_id -> "double" | "blank") applied to the current rules for the mutation checks."""
    damage = damage or {}
    main = json.loads((EVAL / f"items_{name}.json").read_text())["rules"]["main"]
    sealed = {}
    for part in bag_eval.gate_parts(name):
        sealed |= json.loads((EVAL / f"pred_{main}_{part}.json").read_text())
    by_main: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        price = {o[0]: o[2] for o in r["options"]}.get(r["drawn"])
        by_main[r["stratum"]].append(
            (r["store"], bag_eval.gate_outcome(sealed[r["coffee_id"]], r["labels"], r["drawn"], price))
        )
    now = bag_eval.gate_outcomes(rows, lambda r: _rules_grams(r, damage.get(r["coffee_id"])))
    return bag_eval.gate_result({"main": by_main, "now": now}, bag_eval.gate_sizes(name), "main", "now")


def _publishes(r: dict) -> bool:
    listing = {"variants": [{"variant_id": o[0], "price": o[2], "available": o[4]} for o in r["options"]]}
    return bag_eval.published(listing, _rules_grams(r))[1] is not None


def _lowest(ids, k: int) -> list[str]:
    return sorted(ids, key=lambda c: hashlib.sha256(c.encode()).hexdigest())[:k]


GATE_SETS = sorted(_gate_sets()) or ["none"]
NO_GATE = pytest.mark.skipif(not _gate_sets(), reason="no scored gate set yet (eval/bag_grams/README.md)")


@NO_GATE
@pytest.mark.parametrize("name", GATE_SETS)
def test_full_population_accuracy_meets_the_gate(name):
    res = _gate(name, _gate_sets()[name])
    acc, rise = res[("now", "accuracy")][0], res[("paired", "wrong")][0]
    assert acc >= bag_eval.GATE_ACCURACY, (
        f"full-population accuracy {acc:.4f} < {bag_eval.GATE_ACCURACY} (eval/bag_grams/README.md)"
    )
    assert rise <= bag_eval.GATE_RISE, (
        f"full-population wrong rise {rise:.4f} > ceiling {bag_eval.GATE_RISE} (eval/bag_grams/README.md)"
    )
    main_wrong, now_wrong = res["changed_wrong"]
    assert now_wrong <= main_wrong, (
        f"changed-stratum wrong {now_wrong} > ceiling {main_wrong} (eval/bag_grams/README.md)"
    )


@NO_GATE
@pytest.mark.parametrize("name", GATE_SETS)
def test_the_gate_fails_when_one_percent_of_answers_are_damaged(name):
    # The mutation check (review M2, L1 of #139): double the weights of 1% of the gate set's coffees, drawn from
    # those the current rules publish a price for (the lowest sha256 of their ids), and the gate must fail.
    rows = _gate_sets()[name]
    k = max(1, round(0.01 * len(rows)))
    damage = dict.fromkeys(_lowest([r["coffee_id"] for r in rows if _publishes(r)], k), "double")
    assert not _gate(name, rows, damage)["passes"], f"{name}: the gate still passes with {k} coffees damaged"


@NO_GATE
@pytest.mark.parametrize("name", GATE_SETS)
def test_the_gate_fails_when_prices_stop_where_it_cannot_see(name):
    # Review G1 of #139: a rule that stops publishing on the coffees whose drawn option nobody labeled must not pass
    # (an unlabeled blank counts against accuracy, not as rightly blank).
    rows = _gate_sets()[name]
    damage = {r["coffee_id"]: "blank" for r in rows if r["drawn"] not in r["labels"] and _publishes(r)}
    assert damage, f"{name}: no coffee to blank"
    assert not _gate(name, rows, damage)["passes"], (
        f"{name}: the gate still passes with {len(damage)} coffees blanked"
    )


@NO_GATE
@pytest.mark.parametrize("name", GATE_SETS)
def test_the_gate_fails_when_the_changed_coffees_are_damaged(name):
    # Review G2 of #139: the census of the coffees the change publishes differently is a condition of its own, so
    # doubling their weights fails the gate although they weigh too little to move the weighted figures.
    rows = _gate_sets()[name]
    damage = {r["coffee_id"]: "double" for r in rows if r["stratum"] == "changed"}
    if not damage:
        pytest.skip(f"{name} has no census part")
    res = _gate(name, rows, damage)
    assert not res["passes"], (
        f"{name}: the gate still passes with the changed coffees damaged ({res['changed_wrong']})"
    )


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_accuracy_does_not_regress(name):
    acc, wrong, shown = _scores(name)
    # Only train's misses are shown: validation is judged on its totals, never example by example.
    misses = "\n".join(shown) if name == "train" else ""
    assert acc >= FLOOR[name], f"{name}: weighted accuracy {acc:.4f} < floor {FLOOR[name]}\n{misses}"
    assert wrong <= CEILING[name], f"{name}: weighted wrong {wrong:.4f} > ceiling {CEILING[name]}\n{misses}"
