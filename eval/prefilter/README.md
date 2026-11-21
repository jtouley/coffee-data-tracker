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
- **Against the served release.** Both are measured against what customers get now: a filter only in the control
  plane (in a draft, never published) has excluded nothing, so removing it puts nothing back and counts nothing, and
  the Filters page marks it "in draft".

The draft build then counts what actually changed in `public_coffee` (`release_changes`) for those products. Publish
adds that number to the running total since the last full evaluation (`control.step9_total`).

**It always counts, over 50 too.** The census below is a review, not a full evaluation: no held-out split, one
reference labeler, no sealed score. So it never restarts the total. Only a recorded full evaluation of the classifier
does that.

**So the first change over 50 locks the drafts after it.** Once a filter change takes the running total past 50 and
is published (a reviewed add, or a removal, which never waits), every later draft needs a full evaluation first,
whatever it changes, until one is recorded on Release. That is CLAUDE.md step 9 as written: "a total over 50 needs
the full process". The review lets that one change through; it doesn't stand in for the evaluation the drafts after
it need. Release says so beside the check and in the preview.

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
  - **Classify these** (on Release) starts the classify job for exactly the versions the draft build's census lists
    as taken out with no verdict at all (`classify --prefilter-build`, `verdict.review_rows`). The confirm counts
    the same rows, so what it says is what is sent, less any already in flight. It goes through the Jobs path, with
    its guards, window and confirm, at the batch price. Once the draft is built again, they appear with Claude's
    call. An unsure verdict isn't sent again: the owner marks it.
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

Each weekly classify run sends Claude a sample of the week's new product versions that the published filters
excluded, as if they hadn't been excluded.
- **The population:** versions with no verdict, first seen in the run's week (`verdict.this_week`), that a published
  filter excludes and no rule of the code does. Older versions already had their week.
- **How many:** `SHADOW_SAMPLE = 20` by default, a code constant.
- **How they're drawn:** a salted hash of the version (`input_hash`), grouped by roaster. Roasters are taken in the
  order of a salted hash, and one version per roaster in turn, so a single store can't fill the sample; that is
  about equal allocation across stores. The week is in the salt, so each week draws afresh; the salt is
  `SHADOW_SALT`. A version two stores list belongs to the same roaster every time.
- **Claude's answer only reports.** It is stored like any verdict, and never changes the exclusion: the filter
  decides first.
- **What's recorded:** each sampled version, the filter that excluded it, the model and definition, and the size of
  its stratum: how many versions that filter excluded at that store that week (`stratum_size`). Table:
  `prefilter_samples`.
- **The estimate is weighted by store** (CLAUDE.md step 7). The sample takes about the same number from each store,
  so an unweighted share would let small stores speak for large ones. Each (week, store) stratum's share Claude
  called coffee is weighted by its stratum size, and the interval is Wilson's at that share with Kish's effective
  sample size (`admin.queries.wilson_weighted`). An unsure answer counts as neither coffee nor not coffee, and a
  stratum with no answer drops out. The Filters page shows the weighted share, its interval and how many answered.
- **It fails open.** A problem drawing or recording the sample is logged and returned with the run's result. The
  weekly classify still sends everything else.
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
