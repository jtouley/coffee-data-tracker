# Roaster screen evaluation

This will measure the roaster screen that [`docs/design/roaster-screen.md`](../../docs/design/roaster-screen.md)
describes: of the discovery candidates it marks "Suggested roaster", how many are roasters that sell their own coffee
by the bag online; how many of those roasters it finds; and how often it suggests a café or a reseller. It follows
CLAUDE.md in full, because the screen decides what the owner is asked to include. `main` suggests nothing.

**Status: provisional rules, measured on train and validation only (the design's PR 3 of 7).** The rules
(`src/coffeedata/screen_rules.py`, version `provisional-1`) are below the ship bar on validation, on very few items.
No test set is drawn and none is scored, there is no floor, and nothing shows a tier to the owner. This folder holds
the labeling definition, the split rule, #130's train and validation labels, and the scorer for those two sets.
#130's spent test set is a regression fixture in
[`tests/fixtures/roaster_screen_130/`](../../tests/fixtures/roaster_screen_130/README.md).

**Running total (CLAUDE.md step 9):** 0 places whose tier changed since the last full evaluation. No real place
has a tier yet: no screen has run. The first full evaluation is the design's PR 5. Add each later change's count
here.

## Definition

[`GUIDE.md`](GUIDE.md): **an actual roaster that sells coffee it roasts, online.** Each item also gets a `kind`
(roaster, cafe, reseller, other). Suggesting a `cafe` or a `reseller` is a confidently wrong suggestion.

The guide is #130's (`3c8af60:eval/screen/GUIDE.md`). Its `label` and `kind` definitions are unchanged, so #130's
labels still answer this guide's question. Four things differ:
- the opening names the new population (`candidates`, which includes places tagged as roasteries);
- "admission" reads "suggestion" in two sentences;
- the third labeler, who settles disagreements, is never shown the two labels it settles, and labels from #130's
  stored evidence instead of visiting the site (Labels, below);
- the rule that two `unsure` labels don't count as agreement is written down (#130's script applied it; its guide
  didn't say so).

## Population and frame

- **#130's frame, which the reused items come from.** US candidates on Overture release `2026-10-21.0`: open places
  tagged `coffee_shop` or `cafe` with a storefront website, one row per domain, not already a roaster by the
  seed's rule and not in the registry. That is 43,655 domains. `frame.json` has sha256
  `dab17bc80753647d77ddd8a28b6d690324db4f65185872e50dcfd60ec8d1c54b` (`3c8af60:eval/screen/README.md`). The frame
  is out of git.
- **This evaluation's frame: not drawn yet.** It will be the lake's `candidates` that aren't in the registry, on
  one Overture release, frozen by a hash recorded here when the sample is drawn (the design's section 5.2). Unlike
  #130's frame, it includes places tagged `coffee_roastery` that the registry doesn't hold.
- **Positives:** the 210 curated roasters in `config/roasters.csv`, labeled like everything else and reported
  separately. Not drawn yet.

## Split

- **Rule:** `n = int(sha256("coffeedata-screen-eval-2026-11-07:split:" + group)[:8], 16) % 100`. `n < 60` is train,
  `n < 80` is validation, and the rest is test.
- **Salt:** `coffeedata-screen-eval-2026-11-07`. It is #130's, kept so that #130's train items stay in train.
- **Group:** the domain's last two labels, or its last three when it ends in a two-letter country code under `co`,
  `com`, `org` or `net`, so one business never sits on both sides. **On a platform host** where every subdomain is
  its own store (`myshopify.com`, `wixsite.com`, `square.site`, `squarespace.com`, `godaddysites.com`,
  `business.site`, `weebly.com`, `wordpress.com`, `webflow.io`, `bigcartel.com`, `company.site`, `mystrikingly.com`,
  `carrd.co`, `square.online`, `myspreadshop.com`), the group is the store's subdomain on it
  (`shop.made-up.myshopify.com` → `made-up.myshopify.com`).
- **Why, and what moved (decided 2026-11-21 by the coordinating Claude Code session, not the owner).** #130 grouped every store on a platform host as one
  business, so every `*.myshopify.com` store hashed to validation (70) and every `*.wixsite.com` store too (79): the
  new test set could never hold a Shopify-subdomain store, which are often small roasters. Grouping by store moved
  three labeled validation items, all on `myshopify.com`. Validation is only used for choosing, so moving them
  costs nothing that has been measured:
  - `va-29875635` (`dobson-creek-coffee-company.myshopify.com`, hash 46) and `va-82960847`
    (`trixiescoffee.myshopify.com`, 11) moved to train, with their labels, settles and strata. They keep their ids;
  - `va-47515545` (`espresso-news.myshopify.com`, 97) is in the test partition now, so it left every file here. Its
    #130 labels are not carried into a test set: if the new test set draws it, it is labeled afresh.

  No train or spent-test item is on a platform host, so nothing else moved.
- New items, the curated roasters and the new test set are assigned by the same rule.

`tests/test_roaster_screen_scaffold.py` checks that every imported item sits in the set its domain hashes to, and
that no group is on two sides.

## What is here

Every file below holds the same JSON as the file at `3c8af60:eval/screen/`, under the same name unless noted,
except for the three moved items (Split, above): the two that moved to train are appended to each train file, and all
three are taken out of each validation file. Otherwise the items, `agreed` and `to_settle` files are byte for byte,
and the four `labels` files and the two `settled_C` files gained a final newline, which this repo's hooks add. [`SHA256SUMS`](SHA256SUMS) pins every JSON file here, and the
scaffold test checks it.

| File | Train | Validation | What it is |
|---|---|---|---|
| `items_<set>.json` | 202 | 197 | The items to label: id, domain, website, name, city, state, Overture tag. #130 drew 100 `coffee_shop` and 100 `cafe` per set (now 102 and 100 in train, 98 and 99 in validation). No stratum and no screen result |
| `labels_<set>_A.json`, `labels_<set>_B.json` | 202 each | 197 each | The two independent labels: label, kind, and a short note |
| `agreed_<set>.json` | 147 (28 in scope, 119 out) | 168 (34 in scope, 134 out) | Items where A and B gave the same label and kind |
| `to_settle_<set>.json` | 55 | 29 | The disagreements, with both labels |
| `settled_<set>.json` | 55 | 29 | The third labeler's settles of those disagreements (Labels, below): the reference for them |
| `settled_C_<set>.json` | 55 | 29 | #130's third labeler's answers (`settled_<set>.json` there). Kept for comparison only |

`sample_train.json` and `sample_validation.json` are #130's too, byte for byte: each item's stratum (tag × whether
#130's screen found coffee) and the stratum sizes. The provisional scores below are weighted by them. Labelers never
see them. The final evaluation draws new strata from the shadow run and doesn't use these.

[`screen_eval.py`](screen_eval.py) scores train and validation. It has no `final` command.

## Labels

- **Who labeled.** Claude subagents, not people. Two labelers per set, A and B, each read the businesses' own
  websites, blind to each other, to the screen and to the strata. #130 doesn't record which model.
- **When.** 2026-11-07, on #130's branch.
- **Agreement** (label and kind): train 147 of 202 (72.8%), validation 168 of 197 (85.3%); #130's own figures, before
  the move, were 146 and 170 of 200 (73.0%, 85.0%).
- **Settling** (the owner's decision, 2026-11-20, in place of the design's D2 default that the owner settles).
  A third labeler, a Claude Sonnet model, settles the 55 train and 29 validation disagreements into
  `settled_train.json` and `settled_validation.json`. It labels each item independently against `GUIDE.md` and
  sees neither A's and B's labels nor C's, nor any screen result.
  - **What it reads** (the owner's decision, 2026-11-21: no agent visits the sites). The item's own fields (id,
    domain, website, name, city, state, Overture tag) and #130's stored screen evidence for it: the home page's
    visible text cut to 2,000 characters, and up to 40 products from the first catalog page (title, vendor, type,
    tags, and each option's title and grams). Nothing from the screen's result (status, platform, coffee count,
    admission, reason) is shown. A first attempt from the item's fields alone was discarded: with no page to read,
    the labeler answered `unsure` for about 65% of items and `in_scope` for none.
  - **This reference shares the screen's evidence, and so its blind spots.** A and B read the live sites; the
    settler reads only what #130's screen stored, which is close to what the rules score. Where that evidence
    misleads the rules (a reseller's page that looks like a roaster's, a roaster whose catalog wasn't read), it can
    mislead the settler the same way. So on the 55 train and 29 validation items it settles, agreement with the
    rules may read high, and **the provisional precision may read high.** Items with no stored evidence (12: 3 train
    and 9 validation, no home-page text and no product) are likely to come back `unsure` and drop out of the scores. A person settling, or a final
    evaluation on the new screen's own evidence, removes this.
  - **How the inputs are made**, from the repository root:
    `python -I eval/roaster_screen/settle_inputs.py <evidence-dir> eval/roaster_screen <out-dir>`. #130's evidence
    is out of git: `evidence_train.jsonl.gz` sha256
    `4ac7c7ee98c9c02dcc164d2a2b537b3175ed8fb845d4df72a9a2d0639160ab57`, `evidence_validation.jsonl.gz`
    `24d056db0bdd3eaed5574b57bbca752e08e2fbcf3624961d54bbf9a18e55b2be`.
  - **Done 2026-11-21.** Train: 1 in scope, 29 out of scope, 25 `unsure`. Validation: 2 in scope, 6 out of scope,
    21 `unsure` (after the move; before it, 24 and 22). Kinds, train: 38 cafe, 10 roaster, 5 other, 2 reseller;
    validation: 18 cafe, 9 roaster, 2 other.
    Why so many `unsure`, in the settler's own account: the 12 items with no stored evidence, and about 25 that sell
    bags under their own name with no stored claim that they roast. The settler's exact model id wasn't recorded.
- **#130's third labeler, C,** also a Claude subagent, settled the same items in #130. Its answers are kept as
  `settled_C_<set>.json` for comparison. The new settles agree with C, label and kind, on 34 of 55 train items
  (61.8%) and 11 of 29 validation items (37.9%).
- **Unsure** items are left out of the scores: an item whose reference is `unsure` counts as neither in scope nor out
  of scope, so it is in no numerator or denominator. That leaves 46 of the 84 settled items without a reference
  (25 train, 21 validation), which shrinks validation the most. Those items are not a random draw: many are places
  selling their own bags, the hard cases, so leaving them out can flatter the scores as well as shrinking them.
- **Still to label**, after the shadow run and once the owner approves the cost (the design's section 5.2): about
  50 train and 75 validation items from the suggested tier, the 210 curated roasters, and a new test set of 300.
  The owner also spot-checks 50 test items. No person has settled or checked any label here so far.

## #130's test set

It was scored once, on 2026-11-07 (`3c8af60:eval/screen/RESULTS.md`), so it can't be a test set again. Its 200 items,
labels, strata and predictions are in `tests/fixtures/roaster_screen_130/`, with #130's `SHA256SUMS`. The new test set
is drawn from the test partition and leaves those 200 domains out.

No test reads the fixture's labels yet: scoring it needs the new screen's evidence for those items, which the
shadow run collects.

## Provisional results (2026-11-21)

**What was measured.** The rules in `screen_rules.py`, on the 202 train and 197 validation items (after the split's
platform-host fix, above), against the evidence #130's screen stored on 2026-11-07 (out of git).

**Three things make every number here provisional.**
1. **The evidence is #130's, not the new screen's.** No site was asked for this PR, so the new job has screened
   nothing. #130's evidence is cut to what the new screen would read: the first 250 products of a Shopify store, the
   first 25 of a WooCommerce store, and no products on any other platform. The design says the final evaluation
   screens every item again in the shadow run and doesn't reuse this evidence.
2. **The reference for the disagreements shares the screen's evidence.** It is `settled_<set>.json` (Labels, above):
   a third labeler that read #130's stored evidence, close to what the rules score. Where that evidence misleads the
   rules it can mislead the settler too, so **precision may read high.** It left 25 train and 21 validation
   disagreements `unsure`, and those are in no score: 177 train and 176 validation items are scored.
3. **The counts are small.** Validation holds 13 suggested items under the rule in the code, against the 100 the
   design asks for. The intervals say so.

**Evidence files:** `evidence_train.jsonl.gz` sha256 `4ac7c7ee98c9c02dcc164d2a2b537b3175ed8fb845d4df72a9a2d0639160ab57`,
`evidence_validation.jsonl.gz` `24d056db0bdd3eaed5574b57bbca752e08e2fbcf3624961d54bbf9a18e55b2be`. The known roasters
are the 3,203 names in `config/roasters.csv`.

**Tuned on train, and only there.** One threshold moved: the share of the page that must be named coffee went from
0.3 to 0.2. That added one in-scope train item to suggested and no out-of-scope item. The other thresholds are the
design's starting values: N = 5, half the coffees in bag sizes, its own name on half or roastery wording, and 2
origins or roast levels on half. The reseller doubts are the design's, unchanged. Train examples were read; no
validation example was.

**Metrics.** Stratum-weighted, with Wilson 95% intervals on the labeled counts (an approximation for a weighted
ratio, as #130 stated). The two items that moved to train keep their #130 stratum and are weighted with train's;
the strata's population estimates are #130's, made under the old grouping (a handful of platform-host domains out of
about 26,000 and 9,000 per partition). Unsure labels are left out. `main` suggests nothing: precision is undefined, recall is 0, and
it has no confidently wrong answer.

### Train (the rule in the code, `full,N=5`)

| Rule | Labeled | Suggested | Precision of suggested | Suggested a café or reseller | Recall, suggested | Recall, suggested + review | Dropped in scope | Review per 1,000 |
|---|---|---|---|---|---|---|---|---|
| full,N=5 | 177 | 8 | 100.0% [67.6%, 100.0%] (8/8) | 0.0% [0.0%, 32.4%] (0/8) | 24.1% [14.7%, 45.7%] (8/29) | 85.8% [65.5%, 92.4%] (24/29) | 0.0% [0.0%, 21.5%] (0/14) | 58 |

### Validation (aggregates only; every candidate of the design's section 4.2)

| Rule | Labeled | Suggested | Precision of suggested | Suggested a café or reseller | Recall, suggested | Recall, suggested + review | Dropped in scope | Review per 1,000 |
|---|---|---|---|---|---|---|---|---|
| full,N=2 | 176 | 14 | 100.0% [78.5%, 100.0%] (14/14) | 0.0% [0.0%, 21.5%] (0/14) | 38.4% [24.8%, 55.1%] (14/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 63 |
| full,N=3 | 176 | 14 | 100.0% [78.5%, 100.0%] (14/14) | 0.0% [0.0%, 21.5%] (0/14) | 38.4% [24.8%, 55.1%] (14/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 63 |
| full,N=5 | 176 | 13 | 100.0% [77.2%, 100.0%] (13/13) | 0.0% [0.0%, 22.8%] (0/13) | 35.5% [22.5%, 52.4%] (13/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 66 |
| no_depth,N=2 | 176 | 15 | 100.0% [79.6%, 100.0%] (15/15) | 0.0% [0.0%, 20.4%] (0/15) | 41.3% [27.1%, 57.8%] (15/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 61 |
| no_depth,N=3 | 176 | 15 | 100.0% [79.6%, 100.0%] (15/15) | 0.0% [0.0%, 20.4%] (0/15) | 41.3% [27.1%, 57.8%] (15/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 61 |
| no_depth,N=5 | 176 | 14 | 100.0% [78.5%, 100.0%] (14/14) | 0.0% [0.0%, 21.5%] (0/14) | 38.4% [24.8%, 55.1%] (14/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 63 |
| no_own_vendor,N=2 | 176 | 7 | 100.0% [64.6%, 100.0%] (7/7) | 0.0% [0.0%, 35.4%] (0/7) | 18.1% [9.8%, 35.0%] (7/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 81 |
| no_own_vendor,N=3 | 176 | 7 | 100.0% [64.6%, 100.0%] (7/7) | 0.0% [0.0%, 35.4%] (0/7) | 18.1% [9.8%, 35.0%] (7/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 81 |
| no_own_vendor,N=5 | 176 | 7 | 100.0% [64.6%, 100.0%] (7/7) | 0.0% [0.0%, 35.4%] (0/7) | 18.1% [9.8%, 35.0%] (7/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 81 |
| own_brand,N=2 | 176 | 19 | 93.0% [68.6%, 97.1%] (17/19) | 0.0% [0.0%, 16.8%] (0/19) | 47.1% [32.0%, 63.0%] (17/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 52 |
| own_brand,N=3 | 176 | 19 | 93.0% [68.6%, 97.1%] (17/19) | 0.0% [0.0%, 16.8%] (0/19) | 47.1% [32.0%, 63.0%] (17/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 52 |
| own_brand,N=5 | 176 | 17 | 98.6% [73.0%, 99.0%] (16/17) | 0.0% [0.0%, 18.4%] (0/17) | 44.2% [29.5%, 60.4%] (16/36) | 80.4% [68.1%, 92.1%] (30/36) | 0.0% [0.0%, 29.9%] (0/9) | 57 |

**Reading it.**
- **The rule in the code (`full,N=5`) is still below the ship bar on validation.** All 13 labeled suggestions are in
  scope, but the Wilson lower bound is 77.2% against a bar of 88%. No suggestion is a café or a reseller (0 of 13,
  upper bound 22.8% against 8%). Recall is 35.5% suggested against 40%; with review it is 80.4%, above the 75% bar.
- **What the split fix changed** (2026-11-21, the same settles): validation lost three items, and the rule in the
  code went from 15 of 15 suggestions [79.6%, 100%] and recall 37.6% to 13 of 13 [77.2%, 100%] and 35.5%.
- **What the new settles changed.** Under #130's third labeler (C), before the move, the same rule read 93.1% [71.7%,
  98.9%] (15 of 16), with one café or reseller among the suggestions; that item's new settle is `unsure`, so it is in
  no score. The new settler agrees with C on 34 of 55 train items (61.8%) and 11 of 29 validation items (37.9%),
  label and kind. Precision rising as items drop out to `unsure` is the caveat in item 2, not evidence that the rules
  improved.
- **No candidate clears the bar**: every lower bound is under 88%. Only `no_depth` at N = 2 or 3 reaches 40% recall
  (41.3%). `own_brand` finds the most (44.2% at N = 5) and is the only rule with an out-of-scope suggestion (one at
  N = 5, two at N = 2 or 3), none of them a café or a reseller.
- **Nothing dropped was in scope** in either set (0 of 14 train, 0 of 9 validation).
- **No rule is chosen here.** The code keeps the design's default, `full,N=5`. The choice on validation, the freeze
  and the sealed test are the design's PR 5, on the new screen's own evidence and at least 100 suggested items a set.
- **Most of the missed roasters weren't read at all.** Of the 197 validation items, 134 are `not_screened`: the site
  didn't answer #130's screen (which ran behind a proxy), refused, or is on a platform the first phase doesn't read.
- **The tuning predates the settles and the move.** The one threshold moved on train was chosen with C's train
  settles as the reference. Under the new settles, every train suggestion is still in scope (8 of 8, one of them a
  moved item).

Under the design's section 5.5, below the precision bar nothing is labeled "Suggested": everything goes to review.

## The Claude stage's evaluation (planned, not run)

`src/coffeedata/screen_verdict.py` asks Claude about the places the rules leave undecided (review without a reseller
doubt), from the stored evidence only, and records `roaster_online`, `not_roaster` or `unsure` with one sentence of
reason. Its decisions are shown in `screen_scores` and **change no tier**. Promoting a place to Suggested on Claude's
word waits for this evaluation, and it must clear the ship bar below. Nothing here has been run, and no model has
been asked.

- **A new sealed set, drawn after a production run.** The population is every place whose latest screen the
  production rules left undecided and that `screen-verdicts` has decided, under one frozen model and prompt version
  (`screen_verdict.model()` and `DEFINITION_VERSION`, recorded here when the set is drawn). Places come from the test
  partition only (the Split rule above), leaving out #130's 200 and every item already labelled here. Strata are
  Claude's label × Overture tag, with sizes frozen at the draw. The target is 300 places, with at least 100 that
  Claude called `roaster_online` so the precision bound is useful. `SHA256SUMS` covers the items, the strata and the
  decisions' file when they are drawn, and nobody reads their examples while developing.
- **The labels.** Two independent labelers, Claude Opus and Claude Sonnet, each blind to the other, to Claude's
  decision and to the strata, label every item against `GUIDE.md`. A third model settles their disagreements, blind to
  both answers. That model is neither of the two labelers and never Claude Haiku, which makes the decisions being
  scored (CLAUDE.md, #254: the tie-breaker is a different model, and the reference never comes from the model under
  test). The labelers read the same stored evidence as the stage, because no agent visits the sites. That shared
  evidence is the same caveat as the settles above, and the results will say so. A person spot-checks 50 items.
- **Scored once.** The scorer refuses until coverage is complete: every sealed item has a stored decision under the
  frozen model and version, with none pending or in flight, and both labels plus a settle where needed. Only then
  does it print totals and Wilson 95% intervals, never an item, as #252's scorer does. After that the set joins the
  regression fixtures.
- **The metrics.** These are stratum-weighted, with `unsure` reported as its own share and left out of precision:
  - precision of `roaster_online`;
  - confidently wrong, meaning `roaster_online` on a place labelled café or reseller;
  - `not_roaster` on a place in scope;
  - the share of undecided places that stay `unsure`.

  Each is compared with the rules alone, where every one of these places is in review.
- **The bar** is the design's section 5.5, applied to the places Claude would promote. Precision is at least 95%
  with a Wilson lower bound of at least 88%, and confidently wrong is at most 3% with an upper bound of at most 8%.
  Below it, the decisions stay shown and unapplied.

## What comes later

| The design's PR | Adds here |
|---|---|
| 4 | The shadow run. The frame hash and the strata, frozen under a named `rule_version` when the sample is drawn |
| 5 | The top-up labels, a person's settles and spot-check, the sealed test set and its `SHA256SUMS` entries, the choice on validation, `screen_eval.py final` (totals and intervals only), `RESULTS.md`, and the floors in `tests/test_roaster_screen_accuracy.py` |

The metrics and the ship bar are in the design's sections 5.4 and 5.5. In short: suggested precision at least 95%
with a Wilson lower bound of at least 88%; at most 3% of suggestions a café or a reseller, with an upper bound of at
most 8%; at most 2% of dropped places in scope; recall at least 40% for suggested, and at least 75% with review.
CLAUDE.md on `main` adds a bar of its own: the change's estimated accuracy over the full population of current items
is at least 95%, measured on a sealed set stratified and weighted over the whole population. PR 5 states how the
screen meets it.

## Checks

```
sha256sum -c SHA256SUMS                                   # from this directory
(cd ../../tests/fixtures/roaster_screen_130 && sha256sum -c SHA256SUMS)
uv run pytest -q tests/test_roaster_screen_scaffold.py    # from the repository root

# The provisional scores. <evidence-dir> holds #130's evidence_<set>.jsonl.gz, which is out of git.
uv run python eval/roaster_screen/screen_eval.py score <evidence-dir> train full,N=5
uv run python eval/roaster_screen/screen_eval.py score <evidence-dir> validation
uv run python eval/roaster_screen/screen_eval.py show  <evidence-dir> train     # train only: each item
uv run python eval/roaster_screen/screen_eval.py agree                          # the two settles compared
```
