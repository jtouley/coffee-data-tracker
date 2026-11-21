# Classifier evaluation, 2026-11-20: split by roaster, sealed test

A new evaluation of `is_coffee` (`products.decide`: Claude's stored verdict where there is one, the rules otherwise),
built by CLAUDE.md's steps 1 to 7. It gates #179 and #182, which change what the classifier reads for the products
they move, and it is the base for issue #190. The earlier evaluation (`eval/sealed`) predates the split rule.

No product data is in git. This folder holds the scripts, the manifests (ids and salted key hashes) and
`SHA256SUMS`. The samples, the labelers' files, the labels and the frozen predictions live outside the repo, in a
data folder (`<data>` below).

## Where the labels live

The owner's chosen home is a **private dev dataset**, `hf://datasets/jtouley/coffee-eval-labels-dev`, with every
file under `dev/`: the three sets, `test_next`, `preds/`, `manifest/` and this folder's `SHA256SUMS`.

- **It is dev and evaluation data only. Nothing in the pipeline reads it.** `tests/test_eval_labels_dev.py` fails if
  any tracked file outside `eval/` names it.
- `score.py` takes it in place of a local folder, and refuses any remote dataset whose name doesn't end in `-dev`.
  It only ever downloads.
- **Status on 2026-11-21: not uploaded yet.** The files are staged with their checksums verified, but the upload was
  stopped by the session's permission check and waits for the owner. Until then `<data>` is the session's
  scratchpad, which is not durable: see "Not verified".

## The data (step 1)

A read-only local copy of the lake (`common.copy_lake`: `snapshots`, `bodies`, `verdicts`, and the releases of
2026-11-09 and 2026-11-16), copied 2026-11-20. Nothing is written to the lake.

- An item is a product (`roaster_id:product_id`) at its latest stored version, runs 2026-10-26 to 2026-11-16:
  109,751 products, 1,897 roasters.
- `verdicts` holds 62,235 rows, all decided 2026-11-04, one definition (`e45965cf2582`). **No verdict has been stored
  since**: `verdict_batches` has 8 submitted batches (the last on 2026-11-17) and 7 collected, all on 11-04. 6,850
  products that the rules don't exclude outright have no verdict for their latest version, so the rules decide them.
- `predict.py` reproduces the build: every one of the 43,390 coffees in the 11-16 release's `dim_coffee` is a
  coffee here too.

## The split (step 2)

`common.set_of`: `sha256("classifier-inputs-2026-11-20:" + roaster_id)`, first 8 hex digits as an integer, mod 100.
Below 60 is train, below 80 validation, the rest test. Every product of a roaster is in one set.

- **Salt:** `classifier-inputs-2026-11-20`.
- **Fixed before any result:** the seven roasters named in issue #190's examples are train (`common.TRAIN_ONLY`).
- **Left out of every sample:** the 1,796 products of the earlier evaluation (`tests/fixtures/classifier_labels.jsonl`,
  `eval/sealed/sample.json`), which the rules were tuned on or scored against.

| Set | Roasters | Products |
|---|---|---|
| train | 1,150 | 65,901 |
| validation | 370 | 21,198 |
| test | 377 | 22,652 |

## The sample (stratified)

`build_sets.py` draws from each set, in the order of `sha256(salt:item_key)`. Strata come from `main`'s own answer
(commit 58983bc), so they are known before any label:

| Stratum | What it is | Drawn per set | train | validation | test |
|---|---|---|---|---|---|
| `kept` | `main` says coffee | 100 | 26,034 | 7,868 | 10,116 |
| `dropped_reclassified` | not coffee now, but a coffee in the 11-09 release and gone from 11-16's (issue #190) | 150 | 1,972 | 686 | 745 |
| `dropped_other` | every other product `main` drops | 100 | 37,376 | 12,607 | 11,533 |
| `moved_179` | generic-adapter products of the 42 `shopify?` stores, which #179 reads from /products.json | 40 | 519 | 37 | 125 |
| `moved_182` | martinhenrycoffee.com, which #182 reads from its pages | 40 | 0 | 0 | 133 |

The last three columns are each stratum's size in the set: the weights of the weighted accuracy. 390, 386 and 430
products were drawn. 150 in `dropped_reclassified` bounds a 3% rate: 0 of 150 has a Wilson upper limit of 2.5%.
One store is all of `moved_182`, and the split puts a store in one set, so that stratum exists only in test.

## The reference labels (step 6)

- **Definition:** `eval/sealed/GUIDE.md`, unchanged.
- **Labelers A and B:** Claude subagents, one separate session per labeler and set (six sessions), each given only
  the guide and the products (`make_blind.py`: roaster name, title, type, tags, bag sizes, the first 1,500 characters
  of the description), B in a different order. Neither saw a prediction, a stored verdict, a stratum or the other's
  labels.
- **Labeler C:** a Claude Sonnet labeler settles the products A and B disagree on, labeling them afresh without
  seeing either label.
- **No person labeled anything.** A spot-check by a person is still to come, as for `eval/sealed`.

| Set | A and B agree | Cohen's kappa | Settled by C | Reference "unsure", left out |
|---|---|---|---|---|
| train | 387 of 390 | 0.982 | 3 | 4 |
| validation | 381 of 386 | 0.971 | 5 | 16 |
| test | 430 of 430 | 1.000 | 0 | 5 |

A and B are the same model reading the same guide, so their agreement is an upper bound on what two people would
reach, and a shared blind spot would not show as a disagreement.

## Scoring (steps 3 to 5 and 7)

```
python predict.py <data>/<set>/sample.json <lake copy> <predictions.json>   # in the checkout being scored
python score.py agree <data> <set>
python score.py score <data> train|validation <name>=<predictions.json>[@<stratum>] ...
python score.py final <data> <name>=<predictions.json>[@<stratum>] ...      # the sealed test, once per name
```

`score.py` prints totals and Wilson 95% intervals only. Per stratum: products kept that aren't coffee, coffees
dropped, the error rate. Overall: accuracy weighted by stratum size, with a stratified normal interval
(Agresti-Coull variance per stratum). **Confidently wrong** is a wrong answer given as certain: decided by a stored
verdict or by the rules' outright exclusions (`classify.excluded`). The other wrong answers are the rules' fallback.

`@<stratum>` scores that stratum on the rules alone. It answers one question: what happens to moved products if
their stored verdicts stop joining. A moved product gets new tags, type, sizes and description, so a new
`input_hash`, and a verdict joins on `input_hash`. It still uses the inputs stored today, so it is a bound on the
verdict's loss, not a score of the new inputs.

## Results

Frozen before the test was scored: `main` 58983bc, #179 fd2b4fe, #182 d03f3a1 (after merging `main` into it), their
test predictions and the test labels (`SHA256SUMS`, commit 52b2fde).

**The three heads give byte-identical predictions on all three samples, and on all 109,751 products**: neither PR
touches `classify.py`, `products.py`, `verdict.py` or the dbt models that feed them. So the scores below are the same
for `main`, #179 and #182.

| Set | Stratum | n | Kept, not coffee | Dropped, coffee | Error (95% CI) | Confidently wrong |
|---|---|---|---|---|---|---|
| train | kept | 99 | 2 | 0 | 2.0% (0.6-7.1) | 0 |
| train | dropped_reclassified | 149 | 0 | 0 | 0.0% (0.0-2.5) | 0 |
| train | dropped_other | 98 | 0 | 2 | 2.0% (0.6-7.1) | 0 |
| train | moved_179 | 40 | 0 | 3 | 7.5% (2.6-19.9) | 0 |
| **train** | **weighted accuracy** | 386 | 2 | 5 | **97.99% (95.39-100)** | 0 |
| validation | kept | 96 | 4 | 0 | 4.2% (1.6-10.2) | 1 |
| validation | dropped_reclassified | 145 | 0 | 0 | 0.0% (0.0-2.6) | 0 |
| validation | dropped_other | 96 | 0 | 3 | 3.1% (1.1-8.8) | 1 |
| validation | moved_179 | 33 | 0 | 0 | 0.0% (0.0-10.4) | 0 |
| **validation** | **weighted accuracy** | 370 | 4 | 3 | **96.59% (93.52-99.67)** | 2 |
| test | kept | 99 | 1 | 0 | 1.0% (0.2-5.5) | 1 |
| test | dropped_reclassified | 148 | 0 | 0 | 0.0% (0.0-2.5) | 0 |
| test | dropped_other | 98 | 0 | 3 | 3.1% (1.0-8.6) | 2 |
| test | moved_179 | 40 | 0 | 1 | 2.5% (0.4-12.9) | 1 |
| test | moved_182 | 40 | 0 | 0 | 0.0% (0.0-8.8) | 0 |
| **test** | **weighted accuracy** | 425 | 1 | 4 | **97.98% (95.40-100)** | 4 |

The moved strata on the rules alone (the bound described above), against the same strata with their verdicts:

| Set | Stratum | With stored verdicts | On the rules alone | Confidently wrong |
|---|---|---|---|---|
| train | moved_179 | 3 of 40 wrong, 7.5% (2.6-19.9) | 5 of 40, 12.5% (5.5-26.1) | 0, 0 |
| validation | moved_179 | 0 of 33, 0.0% (0.0-10.4) | 1 of 33, 3.0% (0.5-15.3) | 0, 0 |
| test | moved_179 | 1 of 40, 2.5% (0.4-12.9) | 11 of 40, 27.5% (16.1-42.8) | 1, 1 |
| test | moved_182 | 0 of 40, 0.0% (0.0-8.8) | 5 of 40, 12.5% (5.5-26.1) | 0, 0 |

Every added error is a product kept that isn't coffee. Weighted over the whole test set the difference is small
(97.98% to 97.84% for #179's stores, to 97.90% for #182's), because the moved strata are 258 of 22,652 products.

### What this does and doesn't say about #179 and #182

- **Measured:** on today's stored inputs the classifier's output is unchanged, on every product. By the rule (test
  score at least `main`'s, no rise in confidently wrong answers) both pass, as a tie.
- **Not measurable offline:** the inputs each PR will store after its first crawl. The lake has no product read both
  ways, and no store was fetched.
- **The risk the bound shows:** the moved products' stored verdicts will stop joining, and no verdict has been stored
  since 11-04. Until verdicts cover the new versions, the rules decide them.
- **How to close it:** crawl the moved stores to staging without publishing, run `predict.py` over the new rows and
  score them against the labels here (`moved_179`, `moved_182`). The labels are of the product, not of its inputs.

### Issue #190

Read on train only; validation as totals.

- **The `dropped_reclassified` stratum holds no dropped coffee in any set:** 0 of 149 (train), 0 of 145 (validation),
  0 of 148 (test), each with an upper limit near 2.5%. The issue's 3.2% came from 40 products and one labeler
  (0.6% to 15.8%); the two are compatible, and the rate is more likely under 2.5% than at 3%.
- **Where coffee is dropped:** `dropped_other` (2 of 98 train, 3 of 96 validation, 3 of 98 test) and `moved_179`. On
  train every dropped coffee is a product with **no stored verdict**, decided by the rules' fallback, which wants two
  signals and gets none from a sparse listing ("Cranberry Creme", "Nkonge - 2022").
- **Cause 1, not a rule:** verdicts stopped being stored on 11-04. On the train roasters 706 products that the rules
  don't exclude have no verdict and are dropped; 557 of them were first seen after 11-04.
- **Cause 2, a rule:** flavored coffees filed under the store's own "Flavored Coffee" type or "Flavored" tag, whose
  titles read as drinks, nuts or sweets ("Pumpkin Latte", "Toasted Almond", "Sticky Bun"). The change in the
  `classifier-flavored-type` branch keeps them.
- **The next test set is drawn.** After this run the test set above is spent for #179 and #182. `draw_next_test.py`
  drew `test_next` from the test roasters, leaving out the 430 products scored here: 450 products (100 kept, 100
  dropped_reclassified, 150 dropped with no verdict, 100 dropped_other), sealed (`SHA256SUMS`) and **not yet
  labeled**. #190's change is scored on it once, after it is frozen.

## Running total (CLAUDE.md step 9)

Products whose `is_coffee` changed since this evaluation (2026-11-20): **0**.
- #179 and #182 as measured here: 0 (identical output on the stored lake). Their effect after a crawl is unmeasured
  and is not counted as 0: up to 681 and 133 products change inputs.

## SHA256SUMS

Written as each file was made, for the test files before the test was scored; on 2026-11-21 it was extended to
every file kept in the dev dataset (the train and validation labels, all predictions, the manifests). Paths are
relative to `<data>` (the dataset's `dev/`). `sha256sum -c` from there checks them all. `make_blind.py` refuses to rebuild a set whose `blind.json`
differs.

## Not verified

- No person has checked a label.
- The labelers' replies were typed into the label files by the coordinating session from each labeler's own reply
  (ids and letters); a typing slip in one labeler's file shows up as a disagreement, one made in both would not.
- `<data>` is not durable. The label files (ids and labels only) and the frozen predictions need a private home
  before the scratchpad is cleared; the samples can be rebuilt from the lake and the manifests while the lake keeps
  these runs.
- The classifier's output on the inputs #179 and #182 will store.
