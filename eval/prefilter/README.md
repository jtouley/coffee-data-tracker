# Keyword filters: how a change is checked

The owner's keyword filters (the admin's Filters page) extend `classify.excluded`. A product whose title, product type
or tags hold a filter's word or phrase is never a coffee and gets no Claude verdict. A filter only ever excludes, and
it matches whole words only. A letter, digit, apostrophe or hyphen next to the phrase means it is part of a longer
word, so the filter is unsure and doesn't match. That includes the typographic ones: `’ ‘ ʼ ′` and the Unicode
hyphens and dashes. Text is read NFKC-normalized and case-folded, so full-width letters and "Straße" read as their
plain forms (`classify.KeywordFilter`).

A filter change changes "how often is the classifier right", so it follows CLAUDE.md. Three checks apply to it:
1. the running total (step 9), which every change counts toward;
2. a census of what the change does to today's catalog, which the owner reviews when the change is large;
3. a weekly sample of what the published filters keep doing to new products.

## 1. CLAUDE.md step 9: every moved coffee counts

The admin counts the public coffees a filter change moves against the served release (`catalog.PrefilterReader`):
- **Adding a filter:** a public coffee it takes out.
- **Removing a filter:** a product it gives back that comes back as coffee: Claude's verdict, else the rules'
  answer without the filter (`product_decisions.unfiltered_is_coffee`). The build decides those products at once, so
  every one of them counts, not only the ones Claude calls coffee.

The draft build then counts what actually changed in `public_coffee` (`release_changes`) for those products. Publish
adds that number to the running total since the last full evaluation (`control.step9_total`).

**It always counts, over 50 too.** The census below is a review, not a full evaluation: no held-out split, one
reference labeler, no sealed score. So it never restarts the total. Only a recorded full evaluation of the classifier
does that.

At 50 or fewer with the running total, the change takes step 9's short path:
- **each product is checked by hand.** The preview lists every public coffee the change moves, grouped by Claude's
  call. Nothing can be saved without the preview of exactly that change, made by the same person, against the
  release served now: the save carries an HMAC token keyed with the admin's session secret.
- **unit tests** cover the matching (`tests/test_prefilter.py`), the store and the gate
  (`tests/test_admin_prefilter.py`), and the census (`dbt/models/unit_tests.yml`).
- **the floors** (`tests/test_classifier_accuracy.py`) run on the code's own rules. Filters live in the owner's
  snapshot, so they don't change the floors.

## 2. The census of a draft (`eval_prefilter`, `eval_prefilter_summary`)

Every draft build lists each **listed** product its filter change moves:
- **excluded:** this build's filters newly take it out (`stg_products.prefilter_new`);
- **restored:** a filter of the served build excluded it and none of this build's does (`prefilter_restored`).

Products an owner's decision settles, and products no store lists any more, are left out. Each row carries Claude's
call: `coffee`, `not_coffee`, or `no_verdict` (no verdict yet, or an unsure one). `eval_prefilter_summary` gives the
plain counts per direction, and the data test `eval_prefilter_reconciles` checks that they add up.

**Counts, not estimates.** This is the whole population of today's catalog that the change moves, not a sample of
it, so there is nothing to estimate and no interval. Agreement isn't reported as a rate. Products with no verdict
aren't "agreement" or "disagreement": they are unknown, and the owner looks at them.

**The rule at Publish** (`admin/writes.py`, `Writes._prefilter_gate`; the threshold is a constant,
`PREFILTER_UNREVIEWED_MAX = 0`):
- **A change that only removes filters never waits.** A removal hands products back to Claude and the rules, the
  normal path. What comes back still counts toward the running total.
- **A change that adds a filter, 50 or fewer** with the running total: the preview's list was the check.
- **A change that adds a filter, over 50:** Publish waits until every product it takes out that **Claude calls
  coffee or hasn't seen** is marked on Release, *Keep excluded* or *Drop the filter*.
  - **Classify these** (on Release) starts the classify job for exactly the products Claude hasn't seen. It goes
    through the Jobs path, with its guards, window and confirm. The job reads the draft's snapshot by id and digest
    and sends those versions in one batch at the batch price. Once the draft is built again, they appear with
    Claude's call. An unsure verdict isn't sent again: the owner marks it.
  - **The marks** are kept in the control plane (`control.prefilter_reviews`, audited) against the product
    **version** (`input_hash`). A rewritten listing needs a new mark.
  - **One "Drop the filter" refuses Publish.** The owner removes that filter (its preview shows what comes back)
    and builds a new draft.
  - **A draft build with no census** (an older build, or a failed report step) refuses Publish over 50.

**No train, validation or test split.** CLAUDE.md's split exists to choose between candidates without overfitting,
and to score the chosen one once on data it never saw. Neither applies here:
- **There is one candidate per draft:** the owner's filter set. There is nothing to fit on a train set and nothing to
  choose on a validation set.
- **The census isn't a sample of anything,** so a sealed test score would estimate nothing it doesn't already count.
- **The drop-and-rebuild cycle is owner review, not tuning.** When the owner marks "Drop the filter", removes the
  filter and builds again, they act on a product they have looked at, as a reviewer would. That makes the census a
  record of reviewed products, never a held-out score. It is also why it never stands in for a full evaluation and
  never restarts the running total.
- **The reference is Claude's stored verdicts.** CLAUDE.md step 6 allows an existing signal as the reference when the
  change isn't that model or prompt, and a keyword filter isn't.
- **The owner's marks are one labeler.** They decide whether this change ships, and are never a sealed test label.

**What the census can't say: how the filter does on future products.** Once a filter is published, every new product
it matches is excluded without a verdict. The census can't measure that; the weekly sample does.

## 3. The weekly sample (`verdict.SHADOW_SAMPLE`, `prefilter_samples`)

Each weekly classify run sends Claude a sample of the new product versions the published filters excluded that week,
as if they hadn't been excluded.
- **How many:** `SHADOW_SAMPLE = 20` by default, a code constant.
- **How they're drawn:** a salted hash of the version (`input_hash`), grouped by roaster. Roasters are taken in the
  order of a salted hash, and one version per roaster in turn, so a single store can't fill the sample. The week is
  in the salt, so each week draws afresh; the salt is `SHADOW_SALT`.
- **Claude's answer only reports.** It is stored like any verdict, and never changes the exclusion: the filter
  decides first.
- **What's recorded:** each sampled version, the filter that excluded it, and the model and definition, in the lake
  table `prefilter_samples`.
- **What the Filters page shows:** for each filter over the last four weeks, how many of its sampled products Claude
  called coffee, with a Wilson 95% interval. This one is a real random sample of the filter's exclusions, so the
  interval estimates how often the filter takes out something Claude would call coffee.
- **Cost:** about 20 extra Claude calls a week at the batch price, on top of the usual classify run.

## Running total

The admin keeps it (`control.step9_total`, shown on Release with each draft). Nothing has been counted for keyword
filters yet: none existed before this change.

## The real data (CLAUDE.md step 1)

How often does a public coffee have no usable verdict? This decides how much the "Claude hasn't seen" review matters.

- **Release:** the served release `20261104T041920-5060ad` (run 2026-11-02), copied read-only from the lake.
- **Population:** 42,110 listed public coffees. Of these, 41,119 matched that week's snapshot batch.
- **Result:** 1,396 had no coffee/not_coffee verdict (none yet, or unsure), which is 3.4%.
- **Not checked:** the other 991 weren't in the one batch copied.

So a filter that takes out 100 public coffees would, on average, include about three that Claude hasn't seen.
