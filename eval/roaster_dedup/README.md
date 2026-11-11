# One store, one roaster (roaster aliases)

The change: roaster_ids whose resolved hosts have the same key (lower case, no scheme, path, port or surrounding
dots, no leading `www`/`www2`/`locations`/`shop`/`store` label) serve one store, whatever their crawl status, unless
the key is a shared host (`dbt/seeds/shared_hosts.csv`: parked and for-sale pages, shorteners, directories,
platforms). All but one canonical id are **aliases**: never crawled and never public. The rule is written once,
`dbt/macros/roaster_alias.sql`, which dbt (`int_roaster_state`) calls and `registry.STATE_SQL` renders. A
hand-checked `alias_of` in `config/roasters.csv` overrides the host (empty today). `main` has no aliases: every
roaster_id is its own store.

The question it answers, and the reference labels answer: **do these two roaster ids serve the same store?**
(`GUIDE.md`). It changes more than 50 public products, so it gets the full process of the root `CLAUDE.md`.

## Running total (products whose output changed since the last full evaluation)

| Change | public_roaster | public_coffee | public_price_event | Total products |
|---|---|---|---|---|
| this change (first evaluation of the field), 11-09 replay | -36 | -993 | -6,369 | **993** |
| review round 1 (sticky grouping across statuses, shared hosts), 11-09 replay | 0 | 0 | 0 | 993 (unchanged) |

The review round (2026-11-11) changes no public row against the first round: the same 36 roasters, 993 coffees and
6,369 price events leave the public tables. It makes two more aliases, both `no_catalog` and never public
(`cowabungaweb.com`, `magnumcoffee.com`), so 38 aliases of 37 stores; `dim_coffee` and `fct_price_event` now leave
aliases out too (-993 and -6,369, the same rows). No other row of any public table differs (`except all` over every column but the release lineage); the market
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
- Two independent labelers (A, B) label every item; a third (C) labels only their disagreements, blind to both.
  Who labelled is recorded in each file's `meta` and under Results (two Claude subagents). The label inputs are `inputs_<set>.json` in the data dir; train and validation labels go to
  `labels/<set>_<A|B|C>.json`, test labels to `sealed/test_<A|B|C>.json`, and `sealed/SHA256SUMS` is then
  rewritten over every file in `sealed/`.
- The evidence shows each id's resolved host, the change's own input. That is deliberate: a redirect is what makes
  two domains one store, and the cases the host rule can get wrong (parked pages, link shorteners, a supplier's
  shop that separate businesses redirect to) are visible in the evidence. The labelers never see the change's
  verdict or `main`'s. **The consequence: precision is close to guaranteed by construction.** `GUIDE.md` counts a
  resolved host that differs only in case or a leading `www.`/`shop.`/`store.` as the same store, which is the
  rule itself, so a pair the rule calls the same store is almost always labelled so. The precision figures confirm
  the merges are real stores; they can't show where the rule fails. Its failure modes are over time (a canonical's
  outage, a newcomer read once: the review's H1 and M2) and shared hosts, which a one-week snapshot whose registry
  is the seed alone can't exercise; dbt unit tests and `tests/test_dbt.py` cover those instead
  (`a_store_keeps_its_aliases_through_an_outage`).
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

Labelled 2026-11-11 by two Claude subagents (claude-opus-5-5), A and B, blind to each other and to both rules'
predictions. They agreed on every item (train 173/173, validation 49/49, test 23/23), so no tiebreak was needed.
**Two subagents of the same model agreeing is weak evidence of independent labels**: they share the model's
judgement, and the guide restates the rule (above). A person's spot-check of the labels is recommended before
the labels are reused (as for the classifier eval's), at least the 39 `same_store` pairs and the H2 pairs. Wilson
95% intervals; recall also weighted by stratum size (`strata.json`: C2, N1 and R1 are samples), the score
command's `stratum-weighted`.

| Set | Rule | Called same store | Precision | Confidently wrong | Recall | Weighted recall |
| --- | --- | --- | --- | --- | --- | --- |
| train | main | 0 | n/a | n/a | 0/29 (0.00–0.12) | 0.000 |
| train | change | 29 | 29/29 (0.88–1.00) | 0/29 (0.00–0.12) | 29/29 (0.88–1.00) | 1.000 |
| validation | main | 0 | n/a | n/a | 0/6 (0.00–0.39) | 0.000 |
| validation | change | 6 | 6/6 (0.61–1.00) | 0/6 (0.00–0.39) | 6/6 (0.61–1.00) | 1.000 |
| test (sealed) | main | 0 | n/a | n/a | 0/4 (0.00–0.49) | 0.000 |
| test (sealed) | change | 4 | 4/4 (0.51–1.00) | 0/4 (0.00–0.49) | 4/4 (0.51–1.00) | 1.000 |

The change ships: on every set it is at least as accurate as `main`, with no confidently wrong merges. `main` calls
nothing the same store, so precision can't compare the two; on accuracy over all items the change is right on
173/173, 49/49 and 23/23 where `main` is right on 144, 43 and 19. The test set is small (23 items, 4 same-store, no
H2 item), so its intervals are wide.

**Review round 1 (2026-11-11).** The first rule grouped only active and pending roasters; it now groups every
status except shared hosts, and its canonical id is sticky. Predictions were regenerated for all three sets from
the new 11-09 replay:

- Test: no sealed prediction changed (`main` 23/23 and the change 23/23 identical to `sealed/pred_*_test.json`,
  whose checksums still pass), so the test score above stands; the set wasn't re-labelled or re-drawn.
- Validation: no prediction changed.
- Train: two predictions changed, the two same-store pairs the first rule missed, both now called the same store
  (27/29 recall before, 29/29 now). The first round's README said they "do not share a resolved host (a rebrand
  and a sub-domain)"; that was wrong. Both are stratum **H2**: they do share a host key
  (`javaboulevard.com|magnumcoffee.com`, `cowabungaroasters.com|cowabungaweb.com`) but one of each pair is
  `no_catalog`, and the first rule grouped active and pending roasters only. The same status filter caused the
  outage bug (H1), so the fix to grouping caught them.
- The split's key: the host key is now the shared macro's (it also strips a scheme, path and surrounding dots). On
  the 11-09 lake it gives all 3,203 roasters the same key as the first round's expression, so the sets are as drawn.

**What the sealed score does and doesn't measure (final review, L7).** The fix round changed *how* the rule decides
(every status groups, shared hosts never do, a sticky ranking), but on the 11-09 data it changed the output of 2
roaster ids only, and of no product: the two H2 aliases below, both `no_catalog` with 0 coffees and never public.
Under the root CLAUDE.md step 9 that is the light path (0 products changed since the full evaluation, at most 50),
so no new test set was drawn. The sealed set has 0 H2 items, so its score measures the round-1 rule and carries over
only because its predictions are identical; the evidence for the round-2 change is train (the two pairs) and the
unit and integration tests. Per step 5, the scored test set now joins the regression fixtures and the next change
to this rule draws a new test set.

Hand-check of the two ids (train pairs, read with their evidence):

| Alias (new) | Canonical | Evidence | Verdict |
|---|---|---|---|
| `cowabungaweb.com` | `cowabungaroasters.com` | the same name, "Cowabunga Coffee Roasters"; cowabungaweb.com's site resolves to cowabungaroasters.com (Squarespace); 0 coffees on both; the registry's towns differ (Hoboken, NJ / Warrington, PA) | same store (A and B agree); no public row moves |
| `magnumcoffee.com` | `javaboulevard.com` | the same name, Magnum Coffee Roastery; javaboulevard.com's shop is shop.magnumcoffee.com (Shopify, 116 coffees), magnumcoffee.com resolves to www.magnumcoffee.com (no catalog); the registry's towns differ (Nunica, MI / a Las Vegas café listing) | same store's site (A and B agree); the id that reads the catalog stays canonical, so its 116 coffees stay public |
