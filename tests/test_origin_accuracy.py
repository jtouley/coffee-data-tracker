"""extract.rules()'s origin_country against eval/origin's hand labels (eval/origin/README.md).

The labels are in git; the listings are private lake data: ORIGIN_EVAL_DIR/listings.json, which
eval/floor_inputs.py rebuilds from the lake, read-only, at the revision the eval was measured on (the sealed test
listings are left out). Scores are weighted by stratum size (eval/origin/floor_strata.json). Only totals are
printed: validation is judged on its total, never example by example.

Agreement: the rules' origin equals the label ("none": the listing names no single origin; an empty origin agrees
with it). Wrong-country: the rules name a country that isn't the label (an empty origin is never wrong: Claude gets
it), so it has a ceiling instead of a floor.
"""

import json
from pathlib import Path

import pytest

from coffeedata.extract import rules
from coffeedata.models import CatalogItem

from .evalfloor import eval_dir

pytestmark = pytest.mark.private_eval
EVAL = Path(__file__).parent.parent / "eval" / "origin"
# Measured 2026-11-11 on main 0cfa4d9 (train 0.9596 / 0.0394, validation 0.8362 / 0.1599). Raise the floors and
# lower the ceilings when the rules improve; never move them the other way to pass.
FLOOR = {"train": 0.959, "validation": 0.836}
CEILING = {"train": 0.040, "validation": 0.160}


def _scores(name: str) -> tuple[float, float]:
    listings = json.loads((eval_dir("ORIGIN_EVAL_DIR") / "listings.json").read_text())
    strata = json.loads((EVAL / "floor_strata.json").read_text())[name]
    labels = {
        k: (None if v == "none" else v)
        for k, v in json.loads((EVAL / f"labels_{name}.json").read_text()).items()
    }
    total = sum(strata["sizes"].values())
    agree = wrong = 0.0
    for st, size in strata["sizes"].items():
        keys = [k for k, s in strata["labelled"].items() if s == st]
        if not keys:
            continue
        got = {}
        for k in keys:
            r = listings[k]
            item = CatalogItem(roaster_id=r["roaster_id"], product_id=k.split(":", 1)[1], handle="", url="",
                               title=r["title"] or "", platform="", tags=r["tags"], body_text=r["body_text"])  # fmt: skip
            got[k] = rules(item).origin_country
        agree += size / total * sum(got[k] == labels[k] for k in keys) / len(keys)
        wrong += size / total * sum(got[k] is not None and got[k] != labels[k] for k in keys) / len(keys)
    return agree, wrong


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_origin_accuracy_does_not_regress(name):
    agree, wrong = _scores(name)
    assert agree >= FLOOR[name], f"{name}: agreement {agree:.2%} < floor {FLOOR[name]:.1%}"
    assert wrong <= CEILING[name], f"{name}: wrong-country {wrong:.2%} > ceiling {CEILING[name]:.1%}"
