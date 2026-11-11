"""The wholesale rule against labeled listings from the real catalog (eval/wholesale/README.md).

The labeled data is private lake data, so it isn't in the repo: WHOLESALE_EVAL_DIR is the eval folder (its work/
holds population.json), which eval/floor_inputs.py rebuilds from the lake, read-only, at the revision the eval was
measured on. The eval-floors workflow runs this; without the folder it skips (fails under REQUIRE_EVALS=1). Validation is
judged on its total only.
"""

import sys
from pathlib import Path

import pytest

from .evalfloor import eval_dir

pytestmark = pytest.mark.private_eval
sys.path.insert(0, str(Path(__file__).parent.parent / "eval" / "wholesale"))

# Weighted by stratum, measured on 2026-10-28; raise these when the rule improves, never lower them to pass.
FLOOR = {"train": {"precision": 0.97, "recall": 0.85}, "validation": {"precision": 0.99, "recall": 0.97}}


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_wholesale_accuracy_does_not_regress(name):
    from score import measure

    m = measure(str(eval_dir("WHOLESALE_EVAL_DIR")), name, "live")
    for metric, floor in FLOOR[name].items():
        assert m[metric] >= floor, f"{name} {metric}: {m[metric]:.1%} < {floor:.1%}"
