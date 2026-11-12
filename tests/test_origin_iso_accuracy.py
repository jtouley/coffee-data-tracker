"""The origin spelling -> ISO 3166-1 mapping against its labeled census (eval/origin_iso/README.md).

Every captured spelling is labeled (eval/origin_iso/labels.json, with the coffees carrying each), so this scores the
whole population, not a sample: the seed (dbt/seeds/origin_country_iso.csv) through the build's own resolution
(dbt/macros/origin_iso.sql, rendered with Jinja), exactly as dbt runs it. Labels and seed are both in git, so this
runs in the ordinary test job.

Floors (CLAUDE.md step 8; only ever raised): measured 2026-11-11 by #144, 149 of 149 spellings agree, 100% of the
21,923 coffees weighted, no wrong mapping. The owner's rule is at least 95% weighted by products; the floor sits at
the measured score. A wrong mapping (a country published where the label is another country or none) has a ceiling of
0: abstaining is a miss, never wrong.
"""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from .evalfloor import floors

EVAL = Path(__file__).parent.parent / "eval" / "origin_iso"
SEED = Path(__file__).parent.parent / "dbt" / "seeds" / "origin_country_iso.csv"
SPELLINGS_AGREE = 149
WEIGHTED_FLOOR = 0.999
OWNER_RULE = 0.95
WRONG_CEILING = 0


def _eval_module():
    spec = importlib.util.spec_from_file_location("origin_iso_eval", EVAL / "origin_iso_eval.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _labels() -> dict:
    return json.loads((EVAL / "labels.json").read_text())


def test_iso_mapping_over_the_labeled_census():
    ev = _eval_module()
    labels = _labels()
    assert len(labels) >= 149 and not [s for s, v in labels.items() if v["label"] == "UNRESOLVED"]
    res = ev.score(labels, ev.predict(list(labels)))
    with floors("origin_iso", "census") as f:
        f.at_least("spellings that agree", res["agree"], SPELLINGS_AGREE, unit="count", n=res["n"])
        f.at_least(
            "agreement weighted by coffees", res["w_agree"] / res["w"], max(WEIGHTED_FLOOR, OWNER_RULE)
        )
        f.at_most("wrong mappings (spellings)", res["wrong"], WRONG_CEILING, unit="count", n=res["n"])
        f.at_most("wrong mappings (coffees)", res["w_wrong"], WRONG_CEILING, unit="count", n=res["w"])
        f.detail = f"wrong: {res['wrongs']}; misses: {res['misses']}"


def test_every_census_spelling_resolves():
    # No spelling the lake has captured is unmapped: the build's warning (origin_countries_are_mapped) stays quiet.
    ev = _eval_module()
    assert [s for s, got in ev.predict(list(_labels())).items() if got == "UNMAPPED"] == []


@pytest.mark.xfail(
    strict=True, reason="17 seed spellings (9 rows here) are in the round-2 blind labelling (README)"
)
def test_every_seed_row_has_a_label():
    # A spelling added to the seed is measured too: it needs a settled label in labels.json first (README).
    labelled = {s.lower() for s in _labels()}
    with SEED.open(encoding="utf-8") as fh:
        rows = [r["raw_value"] for r in csv.DictReader(fh)]
    assert [r for r in rows if r.lower() not in labelled] == []
