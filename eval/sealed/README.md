# Sealed test set for the coffee classifier

600 products drawn from the live lake on 2026-10-27 (300 the rules kept, 300 they dropped), none of them in the
train or validation sets in `tests/fixtures/classifier_labels.jsonl`. Each was labeled twice, independently, by Claude subagents
against `GUIDE.md`, without seeing any prediction; a third labeler settled the 14 disagreements (`settled.json`).

**Floor (since 2026-11-11).** The set was scored once (RESULTS.md) and is now a regression fixture: `tests/test_sealed_accuracy.py` holds `products.decide` over Claude's frozen verdicts to a stratum-weighted accuracy of 0.982, and the rules alone to 0.940.

Rules for using it:

- Don't read the products or labels to tune rules, prompts or model choice. Choose on the validation set.
- Score once, after the classifier is frozen: `python score_sealed.py final <predictions.json>`. It prints only
  totals and 95% intervals.
- `SHA256SUMS` was written as each file was made; `sha256sum -c SHA256SUMS` (from this directory, after adjusting
  the paths) shows nothing changed since.
- After the final score, the set joins the regression fixture and a new sealed set is drawn for the next change.

`pred_rules.json` holds the rules' predictions as of commit c021723, frozen before any score was taken.

**Running total (CLAUDE.md step 9):** 0 products whose `is_coffee` changed since the last full evaluation, which is
the 2026-10-28 sealed score of Claude's verdicts (`RESULTS.md`). Add each later change's count here.
- decoding HTML entities in tags in dbt (`dbt-decode-tag-entities`) changed the tags of 1,087 products (1,090
  versions, 51 roasters) and the output of `is_coffee` of none: built from main and from the change on a local copy of
  the lake (runs 2026-10-26 to 11-02, rules only), `stg_products` and every mart and public table were identical. 0 of
  them in this sealed set.
- turning on Claude's verdicts in production (`COFFEEDATA_VERDICT_MODEL=claude-haiku-5-5`, the model scored on this
  set in `RESULTS.md`; backfill 2026-11-04, release `20261104T041920-5060ad` against the rules-only
  `20261103T203218-956abd`, same lake otherwise) changed the `is_coffee` of 5,033 products (5,063 versions, 980
  roasters): 3,765 from coffee to not coffee and 1,268 from not coffee to coffee. In `public_coffee` 3,757 coffees
  left and 1,255 entered (42,524 rows, from 45,026). Counted on a local copy of the lake by running the rules and
  `products.decide` over every product version, with and without the stored verdicts. 31 of them are in this sealed
  set. Counted as **0** toward the total: this is the change fully evaluated here on 2026-10-28, put into production.
 
  The 5,033 are kept as a record of its production impact.
- `coffee` winning a tie between two verdicts stored in the same second (`stg_verdicts`, `verdict-tie-prefers-coffee`)
  changed **0** products: on a read-only copy of the lake's `verdicts` (2026-11-06: 62,235 rows, all
  `claude-haiku-5-5`, definition `e45965cf2582`) every `input_hash` has one verdict, so there is no tie to break.
  Step 9's shortcut applies although the order changed: with the three labels (`verdict.LABELS`) `label` ascending
  already put `coffee` first, so every pick is `main`'s for any input. 0 of them in this sealed set.
- naming the rule that decided (`classify.exclusion` and `classify.decision`, `products.decide_why`, for
  `stg_products.reason` and the ops table `product_decisions`; `command-center-exports`, #124) changed **0**
  products: `is_coffee` is now `decision()[0]` and `excluded` is `exclusion() is not None`, with the same rules in
  the same order. `main`'s `classify.py` and the change's agreed on every product version of a local read-only copy
  of the lake (runs 2026-10-26 to 11-02: 104,429 distinct versions, 1,895 roasters), on all 1,196 train and
  validation products, and on the public tables' hashes from the #113 fixture lake (built twice with each). 0 of
  them in this sealed set (compared as a count, without reading its products).
