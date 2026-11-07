# Discovery screen evaluation

This measures the screen that admits Overture `coffee_shop` and `cafe` candidates to the roaster registry
(`src/coffeedata/screen.py`): how often an admitted candidate is an actual roaster that sells coffee it roasts online,
how many of those it finds, and how often it admits a café or a reseller. It follows CLAUDE.md in full, because the
screen is a rule that decides what is included. `main` admits none of these candidates.

**Status: waiting for labels.** The items are drawn; nothing is labeled yet. The rule (`screen.RULE`) and its minimum
coffee count (`screen.MIN_COFFEES`) are provisional (`own_brand`, 1) until they are chosen on the validation set.

**Running total (CLAUDE.md step 9):** 0 candidates whose admission changed since the last full evaluation, which is
this one. Add each later change's count here.

## Definition

[`GUIDE.md`](GUIDE.md): **an actual roaster that sells coffee it roasts, online.** Cafés, coffee shops that serve or
sell another roaster's beans, multi-roaster shops and private-label sellers are out of scope. Each item also gets a
`kind` (roaster, cafe, reseller, other); admitting a `cafe` or a `reseller` is a confidently wrong admission.

## Data

- **Population:** the candidates `discover.overture_places()` returns for Overture release `2026-10-21.0` (US, open
  places tagged `coffee_shop` or `cafe`, primary or alternate, with a website that passes `is_storefront_host`, one
  row per domain, not a roaster by the existing rule): 43,737 domains. The 82 already in the seed
  (`config/roasters.csv`) or the lake's `registry` are left out, as the screen leaves them out: **43,655**.
- The lake's `registry` and `probes` were copied read-only (`snapshot_download`, `registry/*`, `probes/*`,
  `marts/dim_roaster.parquet`); both tables had no rows beyond their schema files on 2026-11-07. Nothing was written to
  the remote lake. The frame, the screen evidence and everything derived from them stay out of git.

## Split

- **Rule:** `n = int(sha256("coffeedata-screen-eval-2026-11-07:split:" + group)[:8], 16) % 100`; `n < 60` train,
  `n < 80` validation, the rest test. **Salt:** `coffeedata-screen-eval-2026-11-07`.
- **Group:** the registrable domain (the last two labels; three under a two-letter country code's `co`, `com`, `org`,
  `net`), so one business's sites never sit on both sides. A chain is already one row per domain.
- Made before any screen result was read. Result (coffee_shop / cafe): train 12,917 / 12,917, validation 4,531 /
  4,595, test 4,358 / 4,337.

## Sample

Two stages, each in a fixed order: `rank = int(sha256(salt + ":rank:" + domain)[:8], 16)`, ascending.

1. **Screen:** the first candidates of each tag in each set are screened live with `screen.screen_one` (450 per tag
   and set). The evidence is kept: the probe, the products read (title, vendor, type, tags, sizes, the first 1,500
   characters of each description) and the home page's text. Every candidate rule is scored from this same evidence
   (`screen.decide`), so rules differ only in how they decide, never in what the site said that day.
2. **Draw:** strata are tag × whether the screen found a product the rules call coffee (`coffees >= 1`, the same for
   every candidate rule). From each stratum the first 50 in rank order are drawn. A stratum's size in the set is
   estimated as its tag's size times the stratum's share of that tag's screened candidates; each labeled item is
   weighted by that size over the stratum's labeled count. Without the coffee stratum, a sample by tag alone would
   hold few admissions (the train screen found coffee at about 12 in 100 candidates) and say little about precision.

The items to label are `items_<set>.json` (id, domain, website, name, city, state, Overture tag; no stratum and no
screen result, sorted by id). `sample_<set>.json` holds the stratum sizes, each item's stratum and its domain.

## Labels

Each item is labeled twice, independently, against `GUIDE.md`, and a third labeler settles disagreements
(`python screen_eval.py agree <set>` writes `agreed_<set>.json` and `to_settle_<set>.json`; the settled ones go in
`settled_<set>.json`). **Labelers: not yet assigned** (to be recorded here: people, or Claude and which model).
Unsure items are left out of the scores.

## Protocol

1. **Train:** the only set whose examples were read. From it: the screen counts a coffee for admission only when it
   is also named as coffee in its title, type or tags (on café and gift stores the rules call jams, dessert bars and
   gift boxes coffee), and a known roaster's name is matched in a title only when it is two or more words that aren't
   all coffee words (single-word names such as "Local" and "Origin" matched blend names).
2. **Validation:** `python screen_eval.py choose <data-dir>` scores every rule in `screen.RULES` with N in 1, 2, 3, 5
   and prints aggregates only. The choice is the highest F0.5 (precision weighs more than recall: a café admitted is
   a crawl wasted every week and a wrong row in the catalog), ties to the stricter rule; then `screen.RULE` and
   `screen.MIN_COFFEES` are set and frozen.
3. **Test (sealed):** `SHA256SUMS` covers `items_test.json` and `sample_test.json`; the test evidence's hash is below.
   Nothing of the test set is read while developing: its screen printed only counts. After the freeze, `main`'s and
   the change's predictions are written and hashed, then scored once with `final`, which prints only totals and
   intervals.

## Metrics

Stratum-weighted, each with a Wilson 95% interval on the labeled counts (an approximation: the interval of a
weighted ratio, sized by the unweighted counts):
- **Precision of admission:** admitted candidates that are in scope.
- **Recall:** in-scope candidates that are admitted.
- **Confidently wrong:** admitted candidates labeled `cafe` or `reseller`, as a share of admitted.

`main` admits none: precision undefined, recall 0, confidently wrong 0. The change ships only if its confidently
wrong admits stay low (the owner: "keep reseller admissions low"; when unsure, reject).

## Limits

- The screen ran from a sandbox behind an HTTPS proxy. About a quarter of the train candidates gave no answer
  (robots.txt unavailable, unreachable, HTTP errors); about two thirds of those don't resolve in DNS at all (dead
  domains). The rest include TLS failures, and some may be the proxy's own refusals, which production (no proxy)
  wouldn't see: recall here may be a little lower than in production.
- Labels are made some days after the screen. A site that changed in between counts against the screen.
- Square Online and Wix pages render most text with JavaScript, so roastery wording is rarely seen there.
- The interval of a weighted proportion is approximated, as above.

## Commands

```
python screen_eval.py frame   <data-dir> <data-dir>/pool_candidates.parquet <lake-dir>
python screen_eval.py screen  <data-dir> <set> 450
python screen_eval.py draw    <data-dir> <set> 50
python screen_eval.py agree   <set>
python screen_eval.py predict <data-dir> <set> <rule> <N> <out.json>      # rule "main": admits none
python screen_eval.py score   <set> <pred.json> ...                       # train and validation only
python screen_eval.py choose  <data-dir>                                  # validation: every rule x N
python screen_eval.py final   pred_main_test.json pred_branch_test.json   # once, after the freeze
```

Run them with `PYTHONPATH=src` from the repository root's `eval/screen` (or with the package installed).
`pool_candidates.parquet` is `discover.overture_places("2026-10-21.0")[1]` written to Parquet. Check the sealed files
with `sha256sum -c SHA256SUMS` from this directory.
