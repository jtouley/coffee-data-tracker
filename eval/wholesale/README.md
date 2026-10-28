# Wholesale listings: evaluation

The wholesale rule (`classify.is_wholesale`, and `classify.says_wholesale` for a bag's text) decides which
listings the site, the app and the market counts leave out. This eval measures it on the real catalog. `C0` is the
rule on `main`; `C1` is this change: "whole sale", fraction packs, and tags such as `wholesale-only` or
`B2B Exclusive` (`rules.py`).

**Read the results as weaker evidence than a sealed test.** The steps weren't done in the order `CLAUDE.md`
requires, and the test set was scored twice. Both are explained below.

## Data

- **Lake snapshot:** `hf://datasets/jtouley/coffee-data` at revision `df57f62`, copied into a local, read-only
  folder. The listings' text is private, so it isn't in this repo.
- **Committed here:** the drawn ids (`samples_<set>.txt`) and every label file (`labels_*.json`). These are
  `coffee_id` keys and labels only, as in `eval/origin`.
- **Rebuilt from the lake:** `EVAL_DIR/work/population.json`, with `population.py`. It is sorted by `coffee_id`,
  so it is the same file on every run, and `SHA256SUMS` pins it together with the committed files.
- **Population:** all 44,190 listed coffees in the snapshot's `dim_coffee` export. Each comes with its latest
  snapshot's title, product type, tags and bag sizes.
- **Split:** `sha256("wholesale-eval-2026-10-28" + roaster_id)` read as a fraction. Below 0.6 is train, below 0.8
  is validation, and the rest is test, so one roaster's listings never sit on both sides. The sets hold 25,718,
  8,718 and 9,754 listings.
- **Strata** (`rules.stratum`), each sampled at its own rate, so precision and recall are weighted by stratum size:
  - **F:** flagged by C0;
  - **B:** not flagged, but bulk-looking (a bulk, case, pack or cafe word in the title, or every bag at 1.8 kg or
    more);
  - **N:** flagged only by C1;
  - **R:** the rest, which isn't sampled.
- **Samples:**
  - train: 120 from F, 120 from B and 40 from N;
  - validation: 50 from each of F and B, and all 33 in N;
  - test: 50 from each of F, B and N.
- **Draws can't be repeated.** These samples were drawn when `population.py` had no fixed order, and the seeded
  shuffle depends on that order. So they can't be drawn again; their ids are committed instead. Draws are
  repeatable from now on.

## Labels

The definition is in `GUIDE.md`. Each of the 563 sampled listings was labeled twice, independently. **All three
labelers were Claude Opus 5.5 sessions, and no person has checked the labels.**

- **A:** the session that wrote the rule change.
  - It labeled validation and test without seeing stratum or prediction.
  - But it wrote C1, so it can apply that rule in its head. "Blind" is weak for A.
  - For train, it saw which pool each listing came from.
- **B:** a separate session that had only the guide and the listings.
- **C:** a third such session, which settled the 72 disagreements (87.2% agreement).

`labels_ref.json` is the result: A where A and B agree, C otherwise. **Listings labeled `unclear` (54 of 563) are
left out of precision and recall.** They cluster in tag-only flags. On test, 17 are unclear, and C1 flags 12 of
them, which it hides from browsing.

## What happened, in order (not the required one)

1. **Train:** read the misses and wrote C1 against them.
2. **Validation:** chose C1 over C0 on totals.
3. **Test:**
   - C1 frozen in the working tree: `classify.py` matched `rules.C1` on every listing.
   - The test sample drawn; A labeled it.
   - Test scored with A's labels: C0 93.0% / 80.1%, C1 94.0% / 93.5% (weighted precision / recall).
   - B and C labeled; test scored again with the settled labels.
4. **One commit:** the rule, `GUIDE.md`, the scripts and a `SHA256SUMS` that already covered the labels all landed
   together, in a340740.

So nothing in git shows that the rule or the test sample was frozen before the labels existed. The order rests only
on this account. The test set was also scored twice. It is not a sealed result.

**Next time, follow this order:**
1. Commit the frozen rule.
2. Draw the test set, write both rules' predictions on it, and commit those with their `SHA256SUMS`. Do this before
   any test label exists.
3. Label.
4. Score once.

**The next wholesale change needs a freshly drawn test set,** under a new salt. This one has been scored twice, and
it should join the regression fixtures.

## Results (`labels_ref.json`; weighted by stratum)

`python score.py $EVAL_DIR <set>` re-derives every number below from the committed files. `score.py` also prints
the sample counts and their intervals.

| Set | C0 (main): precision / recall | C1 (this change): precision / recall | C1 sample, Wilson 95% |
|---|---|---|---|
| train | 97.0% / 75.0% | 97.3% / 85.2% | P 141/144 [94.1, 99.3], R 141/160 [82.2, 92.3] |
| validation | 100.0% / 76.7% | 100.0% / 97.4% | P 81/81 [95.5, 100.0], R 81/82 [93.4, 99.8] |
| test (scored twice) | 94.7% / 81.4% | 95.4% / 94.8% | P 86/88 [92.1, 99.4], R 86/90 [89.1, 98.3] |

- **Recall covers strata F, B and N only.** It says nothing about wholesale listings in R, the unsampled 83% of
  test (8,093 listings that no rule flags and that don't look bulk). Sampling about 100 from R would bound it.
- **Unclear labels are excluded** (see above).
- **Confidently wrong answers don't rise:** on test, C1 has the same 2 false positives as C0.
- **Flag counts:** in a rebuild of the snapshot, C1 flags 3,158 of 44,219 listed coffees; C0 flags 2,729.

**By rule part,** C0's flags break down as follows. The tag-only part is the weak one: of the 92 sampled tag-only
flags, 49 are wholesale, 5 retail and 38 unclear. No pattern separated them without losing true wholesale tags.

| Part | Flags |
|---|---|
| tag only | 1,152 |
| title only | 669 |
| title and tag | 312 |
| product type only | 184 |
| all three | 171 |
| product type and tag | 165 |
| title and product type | 76 |

**Known misses** (false negatives that remain):
- case notation ("2 / 5 lb", "3x5lbs");
- portion counts;
- tags like "B2B Sale";
- private-label codes.

**Known false positives:** retail coffees whose tags or collection list include "wholesale".

## What the filter changes on a build of the snapshot

`build_tree.sh` builds each commit's dbt models against the copy, and `deltas.py` compares them.

| | main | a1733aa | this change |
|---|---|---|---|
| `mart_build_profile` listed_coffees | 43,300 | 40,571 | 40,142 (−7.3% vs main) |
| in_stock_coffees | 38,090 | 35,541 | 35,125 (−7.8%) |
| roasters_with_coffee | 1,847 | 1,846 | 1,846 |
| site catalog rows | 44,071 | 41,342 | 40,913 |
| mart_market coffees | 22,092 | 20,757 | 20,632 |
| new arrivals | 117 | 93 | 92 |
| week: price changes / delisted | 372 / 163 | 372 / 162 | 372 / 162 |
| dim_roaster archived | 919 | 919 | 919 |

The first build after merge drops listed coffees by about 7.3% from the served baseline. That uses about a third
of that week's 20% `volume_tolerance`.
