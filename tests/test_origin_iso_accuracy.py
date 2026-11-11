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

import importlib.util
import json
from pathlib import Path

EVAL = Path(__file__).parent.parent / "eval" / "origin_iso"
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


def test_iso_mapping_over_the_labeled_census():
    ev = _eval_module()
    labels = json.loads((EVAL / "labels.json").read_text())
    assert len(labels) >= 149 and not [s for s, v in labels.items() if v["label"] == "UNRESOLVED"]
    res = ev.score(labels, ev.predict(list(labels)))
    weighted = res["w_agree"] / res["w"]
    print(f"spellings {res['agree']}/{res['n']}, weighted {weighted:.4f}, wrong {res['wrong']}")
    assert res["agree"] >= SPELLINGS_AGREE, (res["agree"], res["misses"], res["wrongs"])
    assert weighted >= max(WEIGHTED_FLOOR, OWNER_RULE), weighted
    assert res["wrong"] <= WRONG_CEILING and res["w_wrong"] == 0, res["wrongs"]


def test_every_census_spelling_resolves():
    # No spelling the lake has captured is unmapped: the build's gate (origin_countries_are_mapped) would fail.
    ev = _eval_module()
    labels = json.loads((EVAL / "labels.json").read_text())
    assert [s for s, got in ev.predict(list(labels)).items() if got == "UNMAPPED"] == []
