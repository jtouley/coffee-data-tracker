"""The classifier against products labeled by hand (two independent labels each, disagreements settled).

tests/fixtures/classifier_labels.jsonl holds two samples from the live lake, 600 products each: 250 the classifier
kept, 250 it dropped and 100 borderline titles. "dev" is the sample the rules were tuned on; "test" was drawn and
labeled afterwards, so its score is the honest one. Green (unroasted) coffee is labeled not coffee; wholesale
bags of one coffee are coffee. A rules change must not lower either score.
"""

import json
from pathlib import Path

import pytest

from coffeedata.classify import is_coffee
from coffeedata.models import CatalogItem, Variant

LABELS = Path(__file__).parent / "fixtures" / "classifier_labels.jsonl"
# Measured on 2026-10-27; raise these when the classifier improves, never lower them to pass.
FLOOR = {"dev": 0.985, "test": 0.935}


def _rows(name):
    return [r for r in map(json.loads, LABELS.read_text().splitlines()) if r["set"] == name]


def _item(r):
    return CatalogItem(
        roaster_id="",
        product_id="",
        handle="",
        url="",
        title=r["title"],
        platform="",
        product_type=r["product_type"],
        tags=r["tags"],
        variants=[Variant(variant_id=str(i), grams=g) for i, g in enumerate(r["grams"] or [])],
        body_text=r["body"],
    )


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_accuracy_does_not_regress(name):
    rows = _rows(name)
    wrong = [r for r in rows if is_coffee(_item(r)) != (r["label"] == "coffee")]
    accuracy = 1 - len(wrong) / len(rows)
    misses = "\n".join(f"  {r['label']:10} {r['title'][:80]}" for r in wrong)
    assert accuracy >= FLOOR[name], f"{name}: {accuracy:.1%} < {FLOOR[name]:.1%}; wrong:\n{misses}"
