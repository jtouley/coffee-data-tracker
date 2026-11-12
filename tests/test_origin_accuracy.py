"""extract.rules()'s origin_country against eval/origin's hand labels (eval/origin/README.md).

The labels are in git; the listings are private lake data: ORIGIN_EVAL_DIR/listings.json, which
eval/floor_inputs.py rebuilds from the lake, read-only, at the revision the eval was measured on (the sealed test
listings are left out). Only totals are printed: validation is judged on its total, never example by example.

Agreement: the rules' origin equals the label ("none": the listing names no single origin; an empty origin agrees
with it). Wrong-country: the rules name a country that isn't the label (an empty origin is never wrong: Claude gets
it), so it has a ceiling instead of a floor.

- Train is weighted by stratum size, as its labels were drawn (eval/origin/floor_strata.json); this reproduces
  RESULTS.md's train row exactly.
- Validation's sample can't be redrawn from any committed rule set (floor_strata.json says why), so RESULTS.md's
  weighted validation numbers (88.0% / 11.6%) can't be reproduced. Its floor is a count over the 111 fixed labelled
  listings instead: a regression check on those listings, not an estimate for the whole set.

Sensitivity: in train the `unchanged` stratum carries 97% of the weight on 25 labels, so one label there moves the
score about 3.9 points, and a regression that touches none of the labelled listings doesn't show.
"""

import json
from pathlib import Path

import pytest

from coffeedata.extract import rules
from coffeedata.models import CatalogItem

from .evalfloor import eval_dir, floors

pytestmark = pytest.mark.private_eval
EVAL = Path(__file__).parent.parent / "eval" / "origin"
# Measured 2026-11-11 on main 0cfa4d9. Train: weighted agreement 0.9596, wrong-country 0.0394 (RESULTS.md: 96.0% /
# 3.9%). Validation: 97 of 111 agree, 11 wrong-country. Raise the floors and lower the ceilings when the rules
# improve; never move them the other way to pass.
# Raised 2026-11-11 by #144: validation 98 of 111 agree, 9 wrong-country (main 0879254 scores 96 / 12 on the same
# labels). One validation label was corrected first, by two blind relabels that agreed (eval/origin/README.md): the
# 787 Coffee listing is Mexican, not "United States"; main had matched the old label by reading customers' cities.
TRAIN_FLOOR, TRAIN_CEILING = 0.959, 0.040
VALIDATION_AGREE, VALIDATION_WRONG = 98, 9


def _predictions(name: str) -> tuple[dict, dict]:
    listings = json.loads((eval_dir("ORIGIN_EVAL_DIR") / "listings.json").read_text())
    labels = {
        k: (None if v == "none" else v)
        for k, v in json.loads((EVAL / f"labels_{name}.json").read_text()).items()
    }
    got = {}
    for k in labels:
        r = listings[k]
        item = CatalogItem(roaster_id=r["roaster_id"], product_id=k.split(":", 1)[1], handle="", url="",
                           title=r["title"] or "", platform="", tags=r["tags"], body_text=r["body_text"])  # fmt: skip
        got[k] = rules(item).origin_country
    return labels, got


def _wrong(got: str | None, label: str | None) -> bool:
    return got is not None and got != label


def test_origin_train_accuracy_does_not_regress():
    labels, got = _predictions("train")
    strata = json.loads((EVAL / "floor_strata.json").read_text())["train"]
    total = sum(strata["sizes"].values())
    agree = wrong = 0.0
    for st, size in strata["sizes"].items():
        keys = [k for k, s in strata["labelled"].items() if s == st]
        if keys:
            agree += size / total * sum(got[k] == labels[k] for k in keys) / len(keys)
            wrong += size / total * sum(_wrong(got[k], labels[k]) for k in keys) / len(keys)
    with floors("origin", "train") as f:
        f.at_least("weighted agreement", agree, TRAIN_FLOOR)
        f.at_most("weighted wrong-country", wrong, TRAIN_CEILING)


def test_origin_validation_accuracy_does_not_regress():
    labels, got = _predictions("validation")
    agree = sum(got[k] == labels[k] for k in labels)
    wrong = sum(_wrong(got[k], labels[k]) for k in labels)
    with floors("origin", "validation") as f:
        f.at_least("listings that agree", agree, VALIDATION_AGREE, unit="count", n=len(labels))
        f.at_most("wrong-country listings", wrong, VALIDATION_WRONG, unit="count", n=len(labels))
