# Keyword filters: how a change is measured

The owner's keyword filters (the admin's Queue, Keyword filters) extend `classify.excluded`. A product whose title,
product type or tags hold a filter's word or phrase is never a coffee and gets no Claude verdict. A filter only ever
excludes. It matches whole words only: next to a letter, digit, apostrophe or hyphen it is unsure, doesn't match, and
the product goes to Claude as before (`classify.KeywordFilter`).

Each change to the filters is a change to "how often is the classifier right", so it follows CLAUDE.md. Which path a
change takes depends on how many public coffees it moves.

## Step 9: 50 or fewer

The admin counts the public coffees a filter change moves against the served release (`catalog.PrefilterReader`):
a public coffee a new filter takes out, or a product a removed filter gives back that Claude calls coffee. It adds them
to the running total since the last full evaluation (`control.step9_total`, the published drafts' counts). The
draft's build then counts what actually changed in `public_coffee` (`release_changes`) for those coffees, and Publish
records that number.

At 50 or fewer with the running total, the change takes CLAUDE.md step 9's short path:
- **Each product is checked by hand.** The preview lists every public coffee the change moves, before anything can be
  saved. Saving without the preview of exactly that change against the release served now is refused.
- **Unit tests** cover the matching rule (`tests/test_prefilter.py`), the store and the gate
  (`tests/test_admin_prefilter.py`).
- **The floors** (`tests/test_classifier_accuracy.py`) run on the code's own rules. Filters live in the owner's
  snapshot, not in code, so they don't change the floors.

## Over 50: measured in the draft build

A change that moves more than 50 public coffees with the running total is measured in its own draft build by the dbt
model `eval_prefilter`. It doesn't wait for a code PR.

- **Reference: Claude's stored verdicts** (`stg_verdicts`). CLAUDE.md step 6 lets an existing signal be the reference
  when the change being measured isn't that model or prompt, and a keyword filter isn't. No new labeling is needed.
- **Population: every product the change newly excludes**, as of its latest record (`stg_products.prefilter_new`).
  That means no rule of the code already excluded it, no filter of the served build did, and no owner's decision
  settles it. This is the whole population, not a sample. There is one frozen candidate (the draft's filters) and
  nothing to tune, so no train, validation or test split is drawn: there is nothing to fit on a train set, nothing to
  choose on a validation set, and no sample whose error a sealed test would estimate.
- **What it reports** (one summary row, then one row per affected product):
  - `affected`: products newly excluded;
  - `claude_not_coffee`: those Claude also calls not_coffee (agreement), with `share_not_coffee` and its Wilson 95%
    interval (`wilson_low`, `wilson_high`);
  - `claude_coffee`: those Claude calls coffee, listed by product. These are the confidently wrong candidates;
  - `no_verdict`: those with no verdict or an unsure one. They are unknown, reported apart and never counted as
    agreement (they stay in the share's denominator).
- **Where it is kept:** beside the draft's release, as an ops table (`releases/.../ops/eval_prefilter.parquet`). It is
  report only: it never fails a build.

**The rule at Publish** (`admin/writes.py`, `Writes._prefilter_gate`):
- it passes when the number of products Claude calls coffee that the owner hasn't reviewed is at most
  `PREFILTER_UNREVIEWED_MAX`. That limit is 0, a constant in code, not a setting;
- the owner reviews each of them on Release, marking it **Keep excluded** or **Drop the filter**. The marks are kept in
  the control plane (`control.prefilter_reviews`, audited, per filter set), so a draft built again on the same
  filters keeps them;
- one **Drop the filter** mark refuses Publish. The owner removes that filter on the Queue (its preview shows what
  comes back) and builds a new draft. A draft never publishes a filter its owner said is wrong;
- a draft build with no `eval_prefilter` (an older build, or a failed report step) refuses Publish over 50: build the
  draft again.

A change measured this way isn't added to the running total. Its own evaluation covers it, and the next full
evaluation of the classifier restarts the total anyway.

**Why this, not `rules.EVALUATED`.** `rules.EVALUATED` is for changes to how a code rule decides (a family, a
polarity, readiness), which need a reviewed evaluation folder merged with the code. A keyword filter changes no code.
It is a list the owner keeps, and its effect on every affected product can be read directly against an existing
reference. The `rules.EVALUATED` path is unchanged for rule-logic changes.

**What it doesn't measure.** It doesn't measure filters' recall (non-coffee they miss: Claude still classifies those),
nor Claude's own error on the agreeing products. Claude was chosen at 98% or better on the validation set
(eval/validation/), so agreement with it is a strong check, not ground truth. The owner's marks on products Claude
calls coffee are one labeler (CLAUDE.md step 6). They decide whether this change ships, and are never a sealed test
label.

## Running total

The admin keeps it (`control.step9_total`, shown on Release with each draft). Nothing has been counted for keyword
filters yet: none existed before this change.
