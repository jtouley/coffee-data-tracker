"""The wholesale rule against labeled listings from the real catalog (eval/wholesale/README.md).

The labeled data is private lake data, so it isn't in the repo: point WHOLESALE_EVAL_DIR at the eval folder (its
work/ holds population.json and the label files) to run these. Validation is judged on its total only.
"""

import os
import sys
from pathlib import Path

import pytest

EVAL_DIR = os.environ.get("WHOLESALE_EVAL_DIR")
pytestmark = pytest.mark.skipif(not EVAL_DIR, reason="WHOLESALE_EVAL_DIR not set (private labeled data)")
sys.path.insert(0, str(Path(__file__).parent.parent / "eval" / "wholesale"))

# Weighted by stratum, measured on 2026-10-28; raise these when the rule improves, never lower them to pass.
FLOOR = {"train": {"precision": 0.97, "recall": 0.85}, "validation": {"precision": 0.99, "recall": 0.97}}


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_wholesale_accuracy_does_not_regress(name):
    from score import measure

    m = measure(EVAL_DIR, name, "live")
    for metric, floor in FLOOR[name].items():
        assert m[metric] >= floor, f"{name} {metric}: {m[metric]:.1%} < {floor:.1%}"
