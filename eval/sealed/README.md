# Sealed test set for the coffee classifier

600 products drawn from the live lake on 2026-10-27 (300 the rules kept, 300 they dropped), none of them in the
train or validation sets in `tests/fixtures/classifier_labels.jsonl`. Each was labeled twice, independently, by Claude subagents
against `GUIDE.md`, without seeing any prediction; a third labeler settled the 14 disagreements (`settled.json`).

Rules for using it:

- Don't read the products or labels to tune rules, prompts or model choice. Choose on the validation set.
- Score once, after the classifier is frozen: `python score_sealed.py final <predictions.json>`. It prints only
  totals and 95% intervals.
- `SHA256SUMS` was written as each file was made; `sha256sum -c SHA256SUMS` (from this directory, after adjusting
  the paths) shows nothing changed since.
- After the final score, the set joins the regression fixture and a new sealed set is drawn for the next change.

`pred_rules.json` holds the rules' predictions as of commit c021723, frozen before any score was taken.

**Running total (CLAUDE.md step 9):** 0 products whose `is_coffee` changed, counted from the change that started
this total; changes before it weren't counted. Add each later change's count here.
- decoding HTML entities in tags in dbt (`dbt-decode-tag-entities`) changed the tags of 1,087 products (1,090
  versions, 51 roasters) and the output of `is_coffee` of none: built from main and from the change on a local copy of
  the lake (runs 2026-10-26 to 11-02, rules only), `stg_products` and every mart and public table were identical. 0 of
  them in this sealed set.
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
- the owner's include/exclude decisions (`stg_decisions`, `stg_snapshots.decision`, macro `decided`; the admin's
  Catalog, `admin-actions`) changed **0** products by themselves: with no decisions snapshot, `decided()` returns
  `stg_products.is_coffee` unchanged, by construction (`stg_decisions` has no rows, so `stg_snapshots.decision` is
  null for every row); every existing dbt and fixture-lake test passes unchanged, and `tests/test_drafts.py` checks a
  draft applies only the decisions it was given. Each decision the
  owner later publishes changes exactly its own products, listed in its draft's `release_changes`; those are per-item
  corrections by the owner, not a rule, so they are not added to this total. 0 of them in this sealed set.
