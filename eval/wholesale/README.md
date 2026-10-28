# Wholesale listings: evaluation

The wholesale rule (`classify.is_wholesale`, and `classify.says_wholesale` for a bag's text) decides which
listings the site, the app and the market counts leave out. This eval measures it on the real catalog, following
`CLAUDE.md`. `C0` is the rule on `main`; `C1` is this change: "whole sale", fraction packs, and tags such as
`wholesale-only` or `B2B Exclusive` (`rules.py`).

## Data

Lake snapshot: `hf://datasets/jtouley/coffee-data` at revision `df57f62`, copied into a local, read-only folder.
The lake is private, so the population, samples and labels aren't in this repo. Only the scripts are here, with
`SHA256SUMS` over the files they wrote, so the same files can be checked later. `EVAL_DIR` holds `lake/` (the
copy), `work/` (the files listed in `SHA256SUMS`) and `trees/` (exported commits, for `build_tree.sh`).

- Population: all 44,190 listed coffees in the snapshot's `dim_coffee` export, each with its latest snapshot's
  title, product type, tags and bag sizes (`population.py`).
- Split: `sha256("wholesale-eval-2026-10-28" + roaster_id)` read as a fraction. Below 0.6 is train, below 0.8 is
  validation, and the rest is test, so one roaster's listings are always in the same set. The sets hold 25,718,
  8,718 and 9,754 listings.
- Strata (`rules.stratum`): F, flagged by C0; B, not flagged but bulk-looking (a bulk, case, pack or cafe word in
  the title, or every bag at 1.8 kg or more); N, flagged only by C1; R, the rest, which isn't sampled. Each
  stratum is sampled at its own rate, so precision and recall are weighted by stratum size. Recall says nothing
  about wholesale listings in R.
- Samples (`sample.py`): train has 120 from F, 120 from B and 40 from N. Validation has 50 from each of F, B and N
  (N held only 33). Test has 50 from each of F, B and N, drawn after C1 was frozen.

## Labels

The definition is in `GUIDE.md`. Each of the 563 sampled listings was labeled twice, independently:
- **A:** the Claude Code session that wrote the change (Claude Opus 5.5). For validation and test it labeled blind
  to stratum and prediction. For train it saw which pool each listing came from.
- **B:** a separate Claude Opus 5.5 session, blind. It had only the guide and the listings.
- **C:** a third Claude Opus 5.5 session, also blind, settled the 72 disagreements (87.2% agreement).

No person has spot-checked the labels yet. `unclear` labels (54 of 563) are reported separately and not counted.

## Procedure

1. Train only: read the misses and wrote C1 against them.
2. Validation: chose C1 over C0 on totals.
3. C1 frozen (`classify.py` matches `rules.C1` on every listing), then the test sample drawn and `SHA256SUMS`
   written. Test was scored once with A's labels, before B and C existed: C0 93.0% / 80.1%, C1 94.0% / 93.5%
   (weighted precision / recall). It was then rescored with the settled labels below. The rule didn't change in
   between.

`python score.py $EVAL_DIR <set>` prints these totals. `tests/test_wholesale_accuracy.py` holds the floors and runs
when `WHOLESALE_EVAL_DIR` is set.

## Results (settled labels; weighted precision / recall)

| Set | C0 (main) | C1 (this change) | Sample: C1 precision, recall (Wilson 95%) |
|---|---|---|---|
| train | 97.0% / 75.0% | 97.3% / 85.2% | 97.9% [94.1, 99.3], 88.1% [82.2, 92.3] |
| validation | 100.0% / 76.7% | 100.0% / 97.4% | 100.0% [95.5, 100.0], 98.8% [93.4, 99.8] |
| test | 94.7% / 81.4% | 95.4% / 94.8% | 97.7% [92.1, 99.4], 95.6% [89.1, 98.3] |

On test, C1 has the same 2 false positives as C0, so confidently wrong answers don't rise. In a rebuild of the
snapshot, C1 flags 3,158 of 44,219 listed coffees, against C0's 2,729.

By rule part, C0's flags break down as tag only 1,152, title only 669, title and tag 312, product type only 184,
all three 171, product type and tag 165, and title and product type 76. The tag-only part is the weak one: of the
92 sampled tag-only flags, 49 are wholesale, 5 retail and 38 unclear. Most of those are roasters that tag
retail-looking coffees "Wholesale", meaning available to wholesale accounts too. No pattern separated them
without losing true wholesale tags, so the rule is unchanged there.

Known misses (false negatives that remain):
- case notation such as "2 / 5 lb" or "3x5lbs";
- portion counts ("126 count", "in-room 200ct");
- tags like "B2B Sale" or "Wholesale Coffee Supplies: ...";
- private-label codes.

Known false positives: retail coffees whose tags or collection list include "wholesale".

## What the filter changes on a build of the snapshot

`build_tree.sh` builds each commit's dbt models against the copy. `deltas.py` compares them. The volume check
metrics (`mart_build_profile`):

| | main | a1733aa | this change |
|---|---|---|---|
| listed_coffees | 43,300 | 40,571 | 40,142 (-7.3% vs main) |
| in_stock_coffees | 38,090 | 35,541 | 35,125 (-7.8%) |
| roasters_with_coffee | 1,847 | 1,846 | 1,846 |
| site catalog rows | 44,071 | 41,342 | 40,913 |
| mart_market coffees | 22,092 | 20,757 | 20,632 |
| new arrivals | 117 | 93 | 92 |
| week: price changes / delisted | 372 / 163 | 372 / 162 | 372 / 162 |
| dim_roaster archived | 919 | 919 | 919 |

The first build after merge therefore drops listed coffees by about 7.3% from the served baseline. That uses
about a third of the 20% `volume_tolerance` that week.
