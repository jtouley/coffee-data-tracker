"""shots.match_coffee against eval/match's train and validation queries (eval/match/README.md).

The reference is mechanical (the coffee a query was made from; an ambiguous title-only query is right only when the
matcher abstains), but the catalog is private lake data: MATCH_EVAL_DIR/lake/marts/dim_coffee.parquet, which
eval/floor_inputs.py fetches from the lake, read-only, at the revision the eval was measured on. 2,000 queries a set
take a few minutes on every CPU, so the eval-floors workflow runs this, not ci. Only totals are printed.
"""

import sys
from collections import Counter
from pathlib import Path

import pytest

from .evalfloor import eval_dir

pytestmark = pytest.mark.private_eval
sys.path.insert(0, str(Path(__file__).parent.parent / "eval" / "match"))

# Measured 2026-11-11 on main 0cfa4d9, the same as #57's RESULTS.md: right 1,621 and 1,632 of 2,000; confidently
# wrong 2 and 3. Raise the floors and lower the ceilings when the matcher improves; never the other way to pass.
FLOOR_RIGHT = {"train": 1621, "validation": 1632}
CEILING_WRONG = {"train": 2, "validation": 3}


@pytest.mark.parametrize("name", sorted(FLOOR_RIGHT))
def test_match_accuracy_does_not_regress(name):
    import match_eval

    lake = str(eval_dir("MATCH_EVAL_DIR") / "lake")
    cat = match_eval.catalog(lake)
    _, drawn = match_eval.draw(cat)
    ambiguous = match_eval.ambiguous_titles(cat)
    tally = Counter(
        match_eval.outcome(cat, ambiguous, key, got) for key, got, _ in match_eval.run(lake, drawn[name], cat)
    )
    assert sum(tally.values()) == 2000
    assert tally["right"] >= FLOOR_RIGHT[name], (
        f"{name}: right {tally['right']} < {FLOOR_RIGHT[name]} of 2,000"
    )
    assert tally["wrong"] <= CEILING_WRONG[name], (
        f"{name}: wrong {tally['wrong']} > {CEILING_WRONG[name]} of 2,000"
    )
