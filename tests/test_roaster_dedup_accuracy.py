"""One store, one roaster (roaster aliases) against eval/roaster_dedup's labels (eval/roaster_dedup/README.md).

Everything it reads is in git: the train and validation pairs and their labels, and the seed list
(config/roasters.csv), whose resolved hosts are the rule's only input on the eval's lake (its registry and probes
held no rows). A pair is called the same store when registry.current gives both ids one canonical_roaster_id, the
macro dbt's int_roaster_state renders too. Only totals are printed.
"""

import json
import sys
from pathlib import Path

import pytest

from coffeedata import registry

ROOT = Path(__file__).parent.parent
EVAL = ROOT / "eval" / "roaster_dedup"
sys.path.insert(0, str(EVAL))

# Measured 2026-11-11 on main 0cfa4d9, as the eval's README reports: every same-store pair called (29/29, 6/6), and
# no pair labelled different called the same store. Raise, never lower, to pass.
FLOOR = {"train": {"called_right": 29, "recall": 1.0}, "validation": {"called_right": 6, "recall": 1.0}}
CEILING_WRONG = {"train": 0, "validation": 0}


@pytest.fixture(scope="module")
def canonical(tmp_path_factory) -> dict[str, str]:
    """Each seed roaster's canonical id, over an empty lake: the seed's hosts alone, as on the eval's lake."""
    from coffeedata.lake import Lake

    lake = Lake(str(tmp_path_factory.mktemp("empty-lake")))
    seed = ROOT / "config" / "roasters.csv"
    return {s.roaster.roaster_id: s.canonical_roaster_id for s in registry.current(lake, seed)}


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_roaster_dedup_accuracy_does_not_regress(name, canonical):
    import dedup_eval

    items = json.loads((EVAL / "items" / f"{name}_items.json").read_text())
    ref = dedup_eval.reference(EVAL / "labels", name)
    pred = {}
    for i in items:
        a, b = i["id"].split("|")
        pred[i["id"]] = canonical.get(a, a) == canonical.get(b, b)
    called = [k for k in pred if pred[k]]
    right = sum(ref[k] == "same_store" for k in called)
    wrong = sum(ref[k] == "different_store" for k in called)
    same = [k for k in ref if ref[k] == "same_store"]
    recall = sum(pred[k] for k in same) / len(same)
    assert right >= FLOOR[name]["called_right"], f"{name}: {right} same-store pairs called"
    assert recall >= FLOOR[name]["recall"], f"{name}: recall {recall:.3f}"
    assert wrong <= CEILING_WRONG[name], f"{name}: {wrong} different-store pairs called the same store"
