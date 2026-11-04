# Data-quality rule evaluations (#11, #12, #7, #4)

Four rules that decide "is this data wrong", measured on held-out data like a model, as the root `CLAUDE.md` asks.
The rules are the dbt tests:

| # | Rule | dbt | Severity |
|---|---|---|---|
| 11 | per-adapter volume bands | `tests/adapter_volume_in_band.sql` (metrics in `mart_build_profile`, bands from `macros/metrics_out_of_band.sql`) | warn |
| 12 | near-duplicate roasters | `tests/near_duplicate_roasters.sql` | warn |
| 7 | price per 100 g outliers within origin and process | `tests/price_per_100g_outliers.sql` | warn |
| 4 | cross-table reconciliation | `tests/public_tables_reconcile.sql`; release against release: `models/marts/release_changes.sql` + `tests/release_changes_explained.sql` | warn |

Results are in "Results" below; how each rule was chosen is under its own heading.

## Data

- A read-only copy of the lake (`hf://datasets/jtouley/coffee-data`), runs 2026-10-26, 10-27, 10-28, 10-30 and 11-02,
  downloaded 2026-11-04. The served release then was `20261103T203218-956abd` (`marts/release.parquet` sha256
  `6a84bfa4…c4cb4e1`). Nothing was written to the remote lake, and no data is in git.
- Each run was replayed: a writable copy of the lake cut off at that run (later partitions of the run-dated tables
  removed; enrichments are not run-dated and were kept whole), then `coffeedata build --run-date <run>` offline with
  no API keys, once on `main` (28a1b05) and once on this branch. The replay of 11-02 reproduces the served build's
  results (WARN 24 orphan events, WARN 6 suspect weeks, 0 errors).
- In git: the labels (`labels/`), the sealed test files (`sealed/`) and the scripts. Out of git: the replayed
  databases and the label inputs (the data dir the scripts write).

## Split

- Rule: `n = int(sha256("coffeedata-dq-eval-2026-11-04:" + key)[:8], 16) % 100`; `n < 60` train, `n < 80`
  validation, else test (`common.split_of`). Salt: `coffeedata-dq-eval-2026-11-04`.
- The key is the roaster: `roaster_id` for a coffee (#7), an adapter cell's roasters (#11: each set has its own
  cells, computed over that set's roasters only) and an orphan event's coffee (#4). For a roaster pair (#12) it is
  the first roaster_id of the pair's cluster (the connected component of the labelled pairs), so a roaster's pairs
  never sit on two sets.
- Roasters with listed coffees on 11-02: train 1,089, validation 386, test 373.
- The split was made before any label or prediction was read.

## Reference labels

- Written definition: `GUIDE.md` (one section per rule).
- Labelers: Claude Opus 5.5 (`claude-opus-5-5`, effort high) through the API, `label.py`: labeler A and labeler B
  each label every item independently (separate runs, each in its own shuffled order, batches of 20, blind to every
  rule's verdict and to each other); labeler C, a third independent run, labels only the items where A and B
  disagree, blind to both, and its label settles them. No person labelled; a person's spot check is still owed.
- Agreement of A and B: #12 149 of 149; #7 343 of 358 (15 settled by C); #11 74 of 75 (1); #4 10 of 10.
- `unsure` counts against precision (a flagged `unsure` is neither right nor wrong) and never as a positive.
- The release-against-release reasons (#4) are mechanical, not judged: each is a fact of the two releases and the
  build (the coffee is in one release and not the other; its latest record is classified as not coffee; ...). As in
  `eval/match`, step 6's double labelling doesn't apply to them.

## Protocol

1. Train: the only set read example by example.
2. Validation: candidates compared on aggregates only.
3. Test (sealed): the rules frozen at the commit that added `sealed/pred_*`; `sealed/SHA256SUMS` was written when
   the test keys and labels were made (before any score), and again when the frozen predictions were added. Scored
   once. `main` and the change are scored on the same frozen set.
4. Ship rule (`CLAUDE.md`): every rule warns first. A rule may block only if its test precision is high and its
   confidently wrong answers don't rise; none is made to block here (see "Severity").

## #12 near-duplicate roasters

- Population: pairs of roasters whose listed, non-wholesale titles overlap (normalized: lower case, runs of
  anything but a-z0-9 to a space). Features: shared titles, shared "rare" titles (listed by at most 3 roasters),
  containment (shared / the smaller list), rare containment.
- Labelled: S1 every pair with at least 2 rare shared titles and rare containment >= 0.1 (70); S2 every other pair
  whose hosts or names are variants of each other (19); S3 a random 60 of the 22,014 other pairs sharing at least 2
  titles (weight 367 each). Pairs sharing 1 title, and not similar by name, weren't labelled.
- Candidates (train): containment >= 0.8/0.9/0.95 with shared >= 3/4/5, size ratio, rare containment. On train
  every containment >= 0.9 candidate is perfect (20/20); validation can't separate them (6/6 each).
- Chosen: shared >= 3, containment >= 0.9 **and** rare containment >= 0.3. Without the rare condition the rule also
  flags 2 unlabelled pairs of small stores sharing 3 generic titles (blackwaterjava/macondo,
  companionroasters/coppercanyon), which no set labelled; the rare condition drops them and nothing labelled.
- On the 11-02 release it lists 37 pairs (the DQ review's 35 groups; Jones has three ids, so three pairs).

## #7 price per 100 g outliers

- Population: listed, non-wholesale coffees with a `price_per_100g` on 11-02 (24,625). Robust z of
  ln(price_per_100g) within (origin_country, process) when that group has >= 30 coffees, else origin_country, else
  all coffees.
- Stratified sample: strata by that z (min group 30): <= -4, -4..-3, -3..-2, -2..2, 2..3, 3..4, 4..6, >= 6; 15 drawn
  per stratum per set (358 labelled). Weights are the stratum sizes (in `price_strata.json`, data dir). The middle
  stratum holds 12,475 / 5,275 / 4,551 coffees per set, so recall estimates rest on 15 labels there and are rough.
- Train showed the low side is nearly always wrong (z <= -3: 27 of 30; mostly "1/2 lb" read as 907 g, a 12 oz
  price on a 5 lb variant, or not coffee) and the high side mixed (4..6: 6 of 15 wrong; >= 6: 9 of 15: bundles priced
  as one bag, pesos and rand, merchandise; the rest real Geshas and Kona).
- Candidates: symmetric |z| >= 3 or 4; low 3 with high 5, 6 or none; low 2.5 / high 6; min group 10/30/100.
- Validation (weighted precision / false-flag share / recall): |z|>=3 0.820/0.147/0.708; low 3 + high 6
  0.957/0.031/0.594; low 3 + high 5 0.935/0.053/0.622; low only 0.973/0.014/0.557. Chosen: **low 3, high 6, min
  group 30** (most of the recall for a 3% false-flag share).

## #11 per-adapter volume bands

What can and can't be measured yet:
- History is five runs, of which two built cleanly (10-26, 11-02); 10-27 (a 50-roaster re-run), 10-28 (mass
  Shopify 429s) and 10-30 (1,000 of 2,545 roasters) fail their floors. The bands need `band_min_builds` (6) passing
  builds per metric, so **in production none of the new bands judges anything before mid-December**, like the
  existing ones.
- So the replay judges each run against the earlier runs that built cleanly, with at least one (for 10-27..11-02
  that is 10-26 alone; MAD 0, so the minimum half-width decides). That measures the minimum half-width and the
  choice of metric; it can't measure band_k or a real MAD.
- Cells: (set, run, adapter) over each set's roasters; judged only when the adapter has >= 20 roasters with a
  catalog. 25 cells per set, 20 judged (10-26 has no history).
- Real collapses in the history: Shopify on 10-28 (ok share 0.14) and WooCommerce on 11-02 (SiteGround 202
  challenges, ok share 0.865), both labelled collapse; Square Online on 10-26 (0.72) is labelled collapse too but is
  unjudged (no history). There is **no real case of a platform returning fewer products, or losing prices, in the
  history**: the 11-02 price wipes were 2 WooCommerce stores of 229 (mozza, hoodriver; their weeks were rejected),
  which a platform-wide ratio can't see (WooCommerce same-store priced 1.0005). The same-store ratios are tested on
  synthetic cases only (`tests/test_dq_rules.py`); on the train and validation cells they flag 0 of 40 judged.
- Main's per-adapter band (`adapter_ok_share`, min half-width 0.05) flags **every** adapter on a run that tried
  fewer roasters (10-27, 10-30): 10 of 12 flags on validation are such cells, labelled normal. The new attempted-ok
  share counts only roasters the run tried (run_is_complete judges the rest).
- Candidates: main's ok share; ok share or attempted-ok share plus mean/median products per fetch, priced share or
  the same-store ratios, at minimum half-widths 0.05 and 0.10. Chosen on validation: **attempted-ok share,
  same-store products and same-store priced, half-width 0.10** (2/2 flags right; at 0.05, 2/3).
- `adapter_ok_share` stays in `build_metrics_in_band` unchanged: replacing it there changes an existing rule and is
  left to the owner.

## #4 cross-table reconciliation

- Within a release (`public_tables_reconcile`): roaster counts against public_coffee rows, the sum of
  dim_roaster.coffees_listed against listed_coffees, a price event's roaster against its coffee's, and orphan price
  events whose product is still coffee. On all five replayed builds: 0 disagreements. The 24 orphan events of 11-02
  (10 coffees) are all products whose latest record is classified not coffee; labelled by_design 10 of 10. Main's
  relationships warning reports all 24; the new rule reports none of them.
- Release against release (`release_changes`): every added, removed or changed row of the four public tables
  against the served release, with a reason. Validation case, the verdict backfill 956abd -> 5060ad (run 11-02),
  rebuilt on this branch with `COFFEEDATA_VERDICT_MODEL=claude-haiku-5-5` and 956abd as the served release (its
  public_coffee, public_price_event and public_market_weekly equal 5060ad's; 3 public_roaster rows differ in
  platform/status from probes newer than 5060ad):
  - public_coffee: added 1,255 (reclassified 1,224, roaster_joined 31); removed 3,757 (reclassified 3,586,
    roaster_left 171); changed 15 (history_changed 15). 45,026 - 3,757 + 1,255 = 42,524.
  - public_price_event: added 8,181 (coffee_joined 8,042, history_changed 139); removed 12,120 (coffee_left
    11,987, history_changed 133).
  - public_roaster: added 1 (first_coffee), removed 16 (no_coffee_left 16), changed 896.
  - public_market_weekly: removed 2 (no_coffees_left: 'Uganda, Kenya, Rwanda, Burundi' washed, 'Sri Lanka'
    unknown), 0 new, changed 79 (coffees_changed 79).
  - **0 unexplained.** The 18 events "on still-public coffees" of the owner's read-only reconciliation came from
    reading the release files with DuckDB's default hive partitioning: the path's `run_date=2026-11-02` replaces
    public_price_event's own run_date, so events of different runs collide. Read with `hive_partitioning = false`
    (as `macros/previous_release.sql` does), 272 events (133 removed, 139 added) sit on 15 still-public coffees,
    and every one of those coffees has a moved history (first_seen or weeks_listed changed): an earlier version is
    now classified as coffee, so its 'new' event moves to the earlier run and a 'price' event appears (e.g.
    caje.coffee "Venus Espresso Blend": first_seen 11-02 -> 10-26, new 124.50 on 10-26, price 124.50 -> 128.50 on
    11-02). The 79 changed cells plus the 2 lost are the owner's 81.
  - Origin normalization, reported and not fixed here: the lost cell 'Uganda, Kenya, Rwanda, Burundi' is a
    multi-country string used as an origin_country.

## Results

Filled in after the single test scoring (see "Commands").

## Severity

Filled in after the test scores.

## Running totals (CLAUDE.md step 9)

These are new rules: they change no product's output (the marts are identical to main's on the 11-02 replay but
for the new mart_build_profile rows and the new release_changes model; mart_dial_in differs in one personal shot's
median, a tie that also varies between two main builds). Count from here: 0 products for each rule.

## Commands

From this directory, with the branch's `.venv`; `<replay>` holds `coffee-<tag>-<run>.duckdb`, `build-<tag>-<run>.log`
and `lake-<tag>-<run>/`, `<data>` is the scripts' data dir:

```
python dupes_eval.py   items <replay>/coffee-main-2026-11-02.duckdb <data>
python price_eval.py   items <replay>/coffee-main-2026-11-02.duckdb <data>
python adapter_eval.py cells <replay> main <data>
python recon_eval.py   items <replay> main <data>
python label.py <dupes|price|adapter|recon> <data>/<rule>_inputs.json <A|B|C> labels/<rule>_<A|B|C>.json [--only <ids>]
python dupes_eval.py   predict <data> '<rule>' <out>          # e.g. 'cont>=0.9,shared>=3,rcont>=0.3'; 'none'
python price_eval.py   predict <db> <data> '<rule>' <out>     # e.g. 'lo=3,hi=6,min_group=30'; 'none'
python adapter_eval.py predict <data> '<rule>' <out>          # e.g. 'ok_attempted:0.1+same_store:0.1+same_store_priced:0.1'
python <rule>_eval.py  score <data> labels <train|validation> <pred>... [--examples N]   # --examples: train only
```

Test scoring, once, totals and Wilson 95% intervals only (check `sealed/` first: `sha256sum -c SHA256SUMS`):

```
python dupes_eval.py   score <data> labels test sealed/pred_dupes_main_test.json sealed/pred_dupes_change_test.json
python price_eval.py   score <data> labels test sealed/pred_price_main_test.json sealed/pred_price_change_test.json
python adapter_eval.py score <data> labels test sealed/pred_adapter_main_test.json sealed/pred_adapter_change_test.json
python recon_eval.py   score <data> labels test
```

The label inputs carry dicts whose key order can differ between two runs of `cells`; the labels were made on the
first run's file.
