# Roaster screen evaluation

This will measure the roaster screen that [`docs/design/roaster-screen.md`](../../docs/design/roaster-screen.md)
describes: of the discovery candidates it marks "Suggested roaster", how many are roasters that sell their own coffee
by the bag online; how many of those roasters it finds; and how often it suggests a café or a reseller. It follows
CLAUDE.md in full, because the screen decides what the owner is asked to include. `main` suggests nothing.

**Status: scaffold (the design's PR 1 of 7). Nothing is scored.** There is no screen code, no scorer, no floor and
no test set yet. This folder holds the labeling definition, the split rule, and #130's train and validation labels.
#130's spent test set is a regression fixture in
[`tests/fixtures/roaster_screen_130/`](../../tests/fixtures/roaster_screen_130/README.md).

**Running total (CLAUDE.md step 9):** 0 places whose tier changed since the last full evaluation. There is no tier
yet, and the first full evaluation is the design's PR 5. Add each later change's count here.

## Definition

[`GUIDE.md`](GUIDE.md): **an actual roaster that sells coffee it roasts, online.** Each item also gets a `kind`
(roaster, cafe, reseller, other). Suggesting a `cafe` or a `reseller` is a confidently wrong suggestion.

The guide is #130's (`3c8af60:eval/screen/GUIDE.md`). Its `label` and `kind` definitions are unchanged, so #130's
labels still answer this guide's question. Four things differ:
- the opening names the new population (`candidates`, which includes places tagged as roasteries);
- "admission" reads "suggestion" in two sentences;
- the third labeler, who settles disagreements, is never shown the two labels it settles;
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
- **Group:** the registrable domain, so one business never sits on both sides. That is the domain's last two labels,
  or its last three when it ends in a two-letter country code under `co`, `com`, `org` or `net`.
- New items, the curated roasters and the new test set are assigned by the same rule.

`tests/test_roaster_screen_scaffold.py` checks that every imported item sits in the set its domain hashes to, and
that no group is on two sides.

## What is here

Every file below holds the same JSON as the file at `3c8af60:eval/screen/`, under the same name unless noted. The
items, `agreed` and `to_settle` files are byte for byte. The four `labels` files and the two `settled_C` files gained
a final newline, which this repo's hooks add. [`SHA256SUMS`](SHA256SUMS) pins all twelve as they are here, and the
scaffold test checks it.

| File | Train | Validation | What it is |
|---|---|---|---|
| `items_<set>.json` | 200 | 200 | The items to label: id, domain, website, name, city, state, Overture tag. 100 `coffee_shop` and 100 `cafe` per set. No stratum and no screen result |
| `labels_<set>_A.json`, `labels_<set>_B.json` | 200 each | 200 each | The two independent labels: label, kind, and a short note |
| `agreed_<set>.json` | 146 (27 in scope, 119 out) | 170 (36 in scope, 134 out) | Items where A and B gave the same label and kind |
| `to_settle_<set>.json` | 54 | 30 | The disagreements, with both labels. **Waiting to be settled** (below) |
| `settled_C_<set>.json` | 54 | 30 | #130's third labeler's answers (`settled_<set>.json` there). Kept for comparison only |

Not imported:
- **`sample_<set>.json`.** Its strata are whether #130's screen found coffee, and #130's evidence isn't reused.
  Every reused item is screened again in the shadow run, and gets its stratum when the new sample is drawn.
- **`screen_eval.py`.** The scorer comes with the evaluation, in the design's PR 5.

## Labels

- **Who labeled.** Claude subagents, not people. Two labelers per set, A and B, each read the businesses' own
  websites, blind to each other, to the screen and to the strata. #130 doesn't record which model.
- **When.** 2026-11-07, on #130's branch.
- **Agreement** (label and kind): train 146 of 200 (73.0%), validation 170 of 200 (85.0%).
- **Settling** (the owner's decision, 2026-11-20, in place of the design's D2 default that the owner settles).
  A third labeler, a Claude Sonnet model, settles the 54 train and 30 validation disagreements into
  `settled_train.json` and `settled_validation.json`. It labels each item independently against `GUIDE.md`, from
  the item's own fields only (id, domain, website, name, city, state, Overture tag): it sees neither A's and B's
  labels nor C's. **Not done yet:** until those two files exist, only the agreed items have a settled reference.
- **#130's third labeler, C,** also a Claude subagent, settled the same items in #130. Its answers are kept as
  `settled_C_<set>.json` for comparison, and its agreement with the new settles is reported when they arrive.
- **Unsure** items are left out of the scores.
- **Still to label**, after the shadow run and once the owner approves the cost (the design's section 5.2): about
  50 train and 75 validation items from the suggested tier, the 210 curated roasters, and a new test set of 300.
  The owner also spot-checks 50 test items. No person has settled or checked any label here so far.

## #130's test set

It was scored once, on 2026-11-07 (`3c8af60:eval/screen/RESULTS.md`), so it can't be a test set again. Its 200 items,
labels, strata and predictions are in `tests/fixtures/roaster_screen_130/`, with #130's `SHA256SUMS`. The new test set
is drawn from the test partition and leaves those 200 domains out.

No test reads the fixture's labels yet: scoring it needs the new screen's evidence for those items, which the
shadow run collects.

## What comes later

| The design's PR | Adds here |
|---|---|
| 3 | `screen_rules.py`'s provisional tiers, and this rule's row in `docs/RULES.md` |
| 4 | The shadow run. The frame hash and the strata, frozen under a named `rule_version` when the sample is drawn |
| 5 | The top-up labels, the owner's `settled_<set>.json`, the sealed test set and its `SHA256SUMS` entries, `screen_eval.py` (its `final` prints only totals and intervals), `RESULTS.md`, and the floors in `tests/test_roaster_screen_accuracy.py` |

The metrics and the ship bar are in the design's sections 5.4 and 5.5. In short: suggested precision at least 95%
with a Wilson lower bound of at least 88%; at most 3% of suggestions a café or a reseller; at most 2% of dropped
places in scope; recall at least 40% for suggested, and at least 75% with review.

## Checks

```
sha256sum -c SHA256SUMS                                   # from this directory
(cd ../../tests/fixtures/roaster_screen_130 && sha256sum -c SHA256SUMS)
uv run pytest -q tests/test_roaster_screen_scaffold.py    # from the repository root
```
