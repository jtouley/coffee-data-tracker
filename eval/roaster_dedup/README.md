# One store, one roaster (roaster aliases)

The change: a roaster_id whose resolved host has the same key as an active roaster's (lower case, no port or
trailing dot, no leading `www`/`www2`/`locations`/`shop`/`store` label) is an **alias** of one canonical id, never
crawled and never public (`dbt/models/marts/dim_roaster.sql`, mirrored by `registry.STATE_SQL`). A hand-checked
`alias_of` in `config/roasters.csv` overrides the host (empty today). `main` has no aliases: every roaster_id is its
own store.

The question it answers, and the reference labels answer: **do these two roaster ids serve the same store?**
(`GUIDE.md`). It changes more than 50 public products, so it gets the full process of the root `CLAUDE.md`.

## Running total (products whose output changed since the last full evaluation)

| Change | public_roaster | public_coffee | public_price_event | Total products |
|---|---|---|---|---|
| this change (first evaluation of the field), 11-09 replay | -36 | -993 | -6,369 | **993** |

No other row of any public table differs (`except all` over every column but the release lineage); the market
tables change only in their counts and prices (71 of 194 `public_market_weekly` cells).

## Data

- The lake `hf://datasets/jtouley/coffee-data` at revision `f52838ec01c4002f0f84f994d23484eb8f797203`
  (2026-11-10, "merge 2026-11-09"), copied read-only. The registry is the seed alone (`config/roasters.csv`; the
  lake's `registry` and `probes` tables hold no rows yet), so every host is the seed's `final_host`.
- Run 2026-11-09 replayed twice, offline with no API keys (`coffeedata build --run-date 2026-11-09` on a writable
  copy of the lake): once on `main` (c6340a8), once on this branch. Both fail only `verdicts_cover_catalog` (no
  verdicts offline for the new week's products), the same on both sides. Nothing was written to the remote lake.
- In git: this README, `GUIDE.md`, `dedup_eval.py`, the train and validation item keys (`items/`), the strata sizes
  (`strata.json`) and the sealed test files (`sealed/`). Out of git: the replayed databases and the label inputs
  (store facts and catalog titles from the private lake), in the data dir `items` writes.

## Items and strata

Every pair of roaster ids (a < b) in the 11-09 replay of `main`, in the first stratum that takes it:

| Stratum | Pairs | Drawn | What |
|---|---|---|---|
| H1 | 37 | all eligible | one host key, both active: the change's merges (35 stores, Jones has 3 ids) |
| H2 | 31 | all eligible | one host key, not both active: parked and dead domains (hugedomains.com, bitly.com, ...), shop subdomains |
| C1 | 115 | all eligible | different host keys, >= 2 shared rare titles (listed by <= 3 roasters): rebrands that don't redirect, white label |
| C2 | 441 | 60 | different host keys, >= 3 shared titles covering half the smaller list |
| N1 | 154 | 60 | different host keys, hosts or names that are variants of each other (eval/dq's rule), both active |
| R1 | 2,762 | 40 | different host keys, two active stores in the same town |

C2, N1 and R1 are random samples (`random.Random(20261110)`), each set's drawn from that set's eligible pairs in
proportion to 60/20/20; scores weight them by their eligible stratum size per set (`strata.json`).

## Split

- Rule: `n = int(sha256("coffeedata-roaster-dedup-2026-11-10:" + key)[:8], 16) % 100`; `n < 60` train, `n < 80`
  validation, else test (`dedup_eval.split_of`). Salt: `coffeedata-roaster-dedup-2026-11-10`.
- The key is the **roaster's host key** (the same key the change groups by), so every id of one store and all of a
  roaster's pairs sit on one set. A pair is an item only when both its roasters are on the same set; a pair across
  sets can't be held out and is left out (most cross-roaster pairs: about 0.36 + 0.04 + 0.04 of them are
  eligible). H1 and H2 pairs share a key, so all of them are eligible.
- The split was made before any label or prediction was read. The rule was chosen once: an earlier draft keyed
  pairs by their connected component, which put one chained component of 150 overlap pairs (white-label hubs) on
  one set; it was replaced by the per-roaster key before any label existed. The salt was not tuned.
- Consequence: big white-label hubs fall on one set, so the overlap strata are uneven across sets (C2 eligible:
  229 / 10 / 2) and the **test set is small**.

| Set | Items | H1 | H2 | C1 | C2 | N1 | R1 |
|---|---|---|---|---|---|---|---|
| train | 173 | 27 | 16 | 34 | 36 | 36 | 24 |
| validation | 49 | 6 | 15 | 1 | 10 | 9 | 8 |
| test (sealed) | 23 | 4 | 0 | 6 | 2 | 3 | 8 |

## Reference labels

- Definition: `GUIDE.md`. Labels: `same_store`, `different_store`, `unsure` (`unsure` counts against precision and
  never as a positive).
- **Not labelled yet.** Two independent labelers (A, B) label every item; a third (C) labels only their
  disagreements, blind to both. Who labels (people, or Claude and which model) is recorded in each file's `meta`
  and here when done. The label inputs are `inputs_<set>.json` in the data dir; train and validation labels go to
  `labels/<set>_<A|B|C>.json`, test labels to `sealed/test_<A|B|C>.json`, and `sealed/SHA256SUMS` is then
  rewritten over every file in `sealed/`.
- The evidence shows each id's resolved host, the change's own input. That is deliberate: a redirect is what makes
  two domains one store, and the cases the host rule can get wrong (parked pages, link shorteners, a supplier's
  shop that separate businesses redirect to) are visible in the evidence. The labelers never see the change's
  verdict or `main`'s.
- `near_duplicate_roasters` (eval/dq #12) used Claude-made labels for overlap pairs; this eval does not reuse them:
  the change is measured on its own fresh draw.

## Protocol

1. Train: the only set read example by example.
2. Validation: candidates compared on aggregates only. One candidate here (the rule as written); `main` as the
   baseline.
3. Test (sealed): `sealed/test_keys.txt` and the frozen predictions `sealed/pred_main_test.json` and
   `sealed/pred_change_test.json` were written, and `sealed/SHA256SUMS` over them, at the commit that froze the
   change, before any label. Scored once.
4. Ship rule: the change's test precision is at least `main`'s (main calls nothing the same store, so it can't be
   wrong that way), with no meaningful rise in confidently wrong answers (`different_store` among pairs called the
   same store); recall is reported, not gated (pairs the host can't link are left to `near_duplicate_roasters`).

## Commands

From this folder, with `DATA` the data dir (outside git) and the two replayed builds:

```
python dedup_eval.py items   main.duckdb $DATA .                      # strata, split, inputs, item keys
python dedup_eval.py predict main.duckdb   $DATA main   $DATA/pred_main_<set>.json   <set>
python dedup_eval.py predict change.duckdb $DATA change $DATA/pred_change_<set>.json <set>
python dedup_eval.py agreement labels <set>                           # A against B
python dedup_eval.py score $DATA labels train      $DATA/pred_main_train.json $DATA/pred_change_train.json
python dedup_eval.py score $DATA labels validation $DATA/pred_main_validation.json $DATA/pred_change_validation.json
```

Test, once, after `cd sealed && sha256sum -c SHA256SUMS`; it prints totals and Wilson 95% intervals only:

```
python dedup_eval.py score $DATA sealed test sealed/pred_main_test.json sealed/pred_change_test.json
```

## Results

Pending the labels. The change calls the same store: train 27 of 173 items, validation 6 of 49, test 4 of 23 (every
H1 pair; no other stratum). `main` calls none.
