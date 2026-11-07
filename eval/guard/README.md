# The guard: evaluation

The guard (`coffeedata.rules`, settings `guard`) decides whether a product the classifier removes from the served
release stays public. This measures it before it may be turned on. It changes public output, so CLAUDE.md asks for
the full evaluation (steps 2-7).

- **The change (C1):** mode `remove_unless_doubted`, `doubted_by: [shared_body]` (`min_products` 3, `min_chars` 60),
  `max_weeks` 4. A removal that `shared_body` doubts is kept as coffee.
- **Main (C0):** every classifier removal is removed. This is the guard `off`, the default.

The two differ only on doubted removals (stratum D below). Everywhere else they give the same answer.

**Status: scored once, 2026-11-07. The guard does not ship** (`RESULTS.md`). The code was frozen at `dd2eb5d` (branch
`admin-rules`, #129); the sets were drawn and both arms' predictions pinned in `SHA256SUMS` before any label existed.

**Who labeled:** Claude subagents, not people. Labelers A and B each labeled all 480 items independently, blind to
each other and to both arms' predictions; a third subagent (C) labeled only the 3 disagreements, blind to A and B.
A spot-check by a person is still to come.

**Running total (CLAUDE.md step 9):** 0 products changed: the guard ships off and no public output has changed
(#129, built row for row against #128's code). Add each later change's count here.

## Data

- **Lake:** a local, read-only copy of `hf://datasets/jtouley/coffee-data` with builds through
  `20261104T041920-5060ad` and the verdict backfill of 2026-11-04 (62,235 verdicts, one model and definition
  `e45965cf2582`). Nothing was written to the remote.
- **The build:** that copy built with the change's code and the guard on, with the served release set to
  `20261103T203218-956abd` (the release before the backfill: the week the Stivers coffees went), as-of run
  2026-11-02. That build fails `classifier_guard` (999 held, over `max_held` 200), which is one of the findings to
  weigh. Its models are what the population reads.
- **Population:** every product the classifier removed from that served release (`flag_verdict_flip`, direction
  `removed`): 3,757 products from 795 stores. Each comes with its latest record's title, product type, tags, bag sizes
  and description (`stg_product_inputs`). `population.py <coffee.duckdb> <EVAL_DIR>` rebuilds it, sorted by
  `coffee_id`, to `EVAL_DIR/work/population.json`. The listings are the stores' text, so they stay out of git.
- **Committed here:** the drawn ids (`samples_<set>.txt`), both arms' predictions (`preds_main.json`,
  `preds_guard.json`), and later the label files. These are `coffee_id` keys and labels only, as in `eval/wholesale`.

## Split

- **Rule:** `sha256("guard-eval-2026-11-07-b" + roaster_id)`, its first 15 hex digits read as a fraction. Below 0.6
  is train, below 0.8 validation, and the rest test. **Salt: `guard-eval-2026-11-07-b`.**
- One store's products never sit on both sides.
- **Sizes:** train 2,283 products (475 stores), validation 845 (177), test 629 (143).
- **A first draw was discarded.** It used the salt `guard-eval-2026-11-07`. While checking the item file's format, one
  test item's title and description were printed. That draw was deleted and the sets drawn again under the new salt
  before anything else was read. No label or prediction of the first draw was looked at.

## Strata and samples

| Stratum | What | train | validation | test |
|---|---|---|---|---|
| D | doubted by `shared_body`: the guard keeps it, main removes it | 576 | 224 | 199 |
| N | not doubted: both remove it | 1,707 | 621 | 430 |
| **Sampled** (`sample.py`, seeded by set and stratum over the sorted population) | | D 100, N 40 | D 100, N 40 | D 150, N 50 |

Accuracy is weighted by stratum size. N is sampled only to report the whole set; the two arms can't differ there.

## Labels (to do: two independent labelers and a tiebreak)

- **The definition:** `GUIDE.md`. It is the classifier's (`eval/sealed/GUIDE.md`) with one note about pasted text.
- **The items:** `EVAL_DIR/work/items_<set>.json`. They are blind: no stratum, no prediction, no verdict, shuffled.
- **Labelers A and B** each label every item of every set on their own, writing `labels_A.json` and
  `labels_B.json` here (`{coffee_id: coffee | not_coffee | unsure}`). **C** labels only the disagreements, in
  `labels_C.json`. Then `python score.py <EVAL_DIR> settle labels_A.json labels_B.json labels_C.json` writes
  `labels_ref.json` and prints the agreement.
- **Record who labeled** (people, or Claude and which model) in this README before scoring.
- **Add `labels_A/B/C.json` and `labels_ref.json` to `SHA256SUMS`** as they are written.

## Protocol

1. **Train:** the only set whose examples may be read. Read it to tune `min_products`, `min_chars` and
   `max_held` if the guard is worth keeping.
2. **Validation:** choose between candidate settings on totals only (`python score.py <EVAL_DIR> validation`).
3. **Test (sealed):** scored once, after the settings are frozen and both arms' test predictions are written again
   for them (a new `SHA256SUMS`). Don't read its items. The command prints totals and intervals only:

   ```
   python score.py <EVAL_DIR> test
   ```

   `sha256sum -c SHA256SUMS` checks the files: the first four from this folder, the two `work/` lines from `EVAL_DIR`.
4. **Ship rule:**
   - the guard ships only if its weighted test accuracy is at least main's;
   - and its confidently wrong answers (products kept public that aren't coffee) don't rise more than its
     confidently right ones (coffees kept that main removes): on D, more than half the held products must be coffee;
   - and a `max_held` is chosen that a normal week passes. The backfill week holds 999.
5. **If it ships:**
   - write `RESULTS.md` with every set's scores, Wilson 95% intervals and the weights;
   - add the guard's entry to `coffeedata.rules.EVALUATED` with the evaluated value (`deciding_part`). Only then
     does the admin's Publish accept a draft that turns the guard on;
   - this test set joins the regression fixtures, and the next change draws a new one under a new salt.

## What the build shows (counts, no labels)

On the build above, the guard holds 999 of the 3,757 removals:
- **Stivers:** 19 of its 21 removals (16 single-origins, 4 sample packs and Chocolate) are doubted by the pasted
  sample-pack paragraph, and held.
- **Drink mixes:** 3 of the 80 removed products whose titles name hot chocolate, cocoa, drink, beverage or frappé
  mix, a mixer or chai are held.
- **Stores that sell no coffee:** 17 stores had every public product removed (172 products); 75 of those would be
  held.

Whether the held ones are mostly coffee is the question the labels answer.
