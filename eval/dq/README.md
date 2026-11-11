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

## Floors

`tests/test_dq_accuracy.py` runs #12 and #7 as dbt compiles them over release `20261103T203218-956abd`'s served `dim_coffee` and `dim_roaster` (private; `eval/floor_inputs.py` fetches them from the lake at b3bf5e8), redraws the samples with this folder's `items` code, checks they are the labelled ones, and holds train and validation to the scores measured 2026-11-11 on main 0cfa4d9 (the Results below): #12 precision and recall 1.0 with no distinct pair flagged; #7 weighted precision 0.857 / 0.957, recall 0.325 / 0.594, false-flag share at most 0.042 / 0.031. #11 and #4 have no floor: #11's inputs are metrics of replayed builds that never existed in the lake, and #4 has 3 labelled items of one label. The eval-floors workflow runs it (`docs/RULES.md`).

## Data

- A read-only copy of the lake (`hf://datasets/jtouley/coffee-data`), runs 2026-10-26, 10-27, 10-28, 10-30 and 11-02,
  downloaded 2026-11-04. The served release then was `20261103T203218-956abd` (`marts/release.parquet` sha256
  `6a84bfa4…c4cb4e1`). Nothing was written to the remote lake, and no data is in git.
- Each run was replayed: a writable copy of the lake cut off at that run (later partitions of the run-dated tables
  removed; enrichments are not run-dated and were kept whole), then `coffeedata build --run-date <run>` offline with
  no API keys, once on `main` (28a1b05) and once on this branch. The replay of 11-02 reproduces the served build's
  results (WARN 24 orphan events, WARN 6 suspect weeks, 0 errors).
- In git: the train and validation labels (`labels/`), the sealed test files (`sealed/`: test keys, test labels,
  frozen predictions, `SHA256SUMS`) and the scripts. The test labels are only in `sealed/`, and test scoring reads
  them from there, so everything a test score uses is hashed. Out of git: the replayed
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
- Agreement of A and B, over all three sets: #12 149 of 149; #7 343 of 358 (15 settled by C); #11 74 of 75 (1);
  #4 10 of 10. A, B and C are blind runs of one model and prompt, not independent labelers, so high agreement says
  little; a person's spot check is still owed.
- `unsure` counts against precision (a flagged `unsure` is neither right nor wrong) and never as a positive.
- The release-against-release reasons (#4) are mechanical, not judged: each is a fact of the two releases and the
  build (the coffee is in one release and not the other; its latest record is classified as not coffee; ...). As in
  `eval/match`, step 6's double labelling doesn't apply to them.

## Protocol

1. Train: the only set read example by example.
2. Validation: candidates compared on aggregates only.
3. Test (sealed): the rules frozen at the commit that added `sealed/pred_*`; `sealed/SHA256SUMS` was written when
   the test keys and labels were made (before any score), and again when the frozen predictions were added. Scored
   once. `main` and the change are scored on the same frozen set. The sums, the test labels and the predictions
   were committed together (bbe214e), so git itself can't show that the sums predate the score; the order above is
   this session's record. Until the fix round the test labels also sat in `labels/`, outside the sums; they were
   removed from there, and the scorers now read the test labels from `sealed/` only.
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
  all coffees. Fix round: a group whose MAD is 0 (z undefined) now also falls back to the wider group, instead of
  never flagging. On the 11-02 replay this changes no coffee's flag (966 before and after; no group of 30 has
  MAD 0), so the running total stays 0 and the sealed scores stand; a fixture covers the case.
- Stratified sample: strata by that z (min group 30): <= -4, -4..-3, -3..-2, -2..2, 2..3, 3..4, 4..6, >= 6; 15 drawn
  per stratum per set (358 labelled). Weights are the stratum sizes (in `price_strata.json`, data dir). The middle
  stratum holds 12,475 / 5,275 / 4,551 coffees per set, so recall estimates rest on 15 labels there and are rough.
- Train showed the low side is nearly always wrong (z <= -3: 27 of 30; mostly "1/2 lb" read as 907 g, a 12 oz
  price on a 5 lb variant, or not coffee) and the high side mixed (4..6: 6 of 15 wrong; >= 6: 9 of 15: bundles priced
  as one bag, pesos and rand, merchandise; the rest real Geshas and Kona).
- Candidates: symmetric |z| >= 3 or 4; low 3 with high 5, 6 or none; low 2.5 / high 6; min group 10/30/100.
- Validation (weighted precision / false-flag share / recall): |z|>=3 0.820/0.147/0.708; low 3 + high 6
  0.957/0.031/0.594; low 3 + high 5 0.935/0.053/0.622; low only 0.973/0.014/0.557. Chosen: **low 3, high 6, min
  group 30** (on validation, most of the recall for a 3% false-flag share; on test the weighted false-flag share was
  0.132 and recall 0.200, see "Results").

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
  (10 coffees) are the items: 3 in train (all labelled by_design, all products whose latest record is classified
  not coffee) and 7 in test, scored in totals only (see "Results"). Main's relationships warning reports all 24;
  the new rule reports none of them.
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
- **That measurement was too narrow** (review of #111, finding 1). The backfill is a same-run rebuild, where
  `weeks_listed` and `last_seen` don't advance; in an ordinary week they advance for every coffee seen, so the
  first version labelled every listed coffee `history_changed` and let a rewritten past price pass as explained.
  The fix compares only business columns (not `weeks_listed`, `last_seen`, release ids or `last_ok_run`), and
  counts a history as changed only when `first_seen` moved or `weeks_listed` moved by other than the coffee's runs
  since the served release. Re-measured after merging main (which now keeps non-US roasters out of the public
  tables, #107):
  - an ordinary week of real data, the 10-26 replay served and 11-02 built: public_coffee added 1,969 (new),
    removed 38 (reclassified 10, roaster_left 28), changed 1,315 (attributes_changed 908, delisted 407), no
    history_changed; public_price_event added 12,904 (coffee_joined 6,783, new_run 6,121), removed 75
    (coffee_left 75); public_roaster added 38 (first_coffee), removed 5 (outside_us 5: aroma, Black Sheep, La
    Cabra, Macaco, The Coffee Roaster, the non-US stores #107 drops), changed 368; public_market_weekly removed 1,
    changed 106. **0 unexplained.** The first run of this check found those 5 roasters `unexplained`: #107's US
    filter was a reason the model didn't know yet, and it now has (`outside_us`).
  - the backfill again, on the merged code: public_coffee +1,254 / -3,787 (reclassified 3,582, roaster_left 205);
    public_price_event +8,178 / -12,204; public_roaster +1 / -21 (no_coffee_left 16, outside_us 5); market -2 cells,
    82 changed. 0 unexplained. (The counts differ from 5060ad's by the 5 non-US roasters, which 5060ad still had.)
  - fixtures: an ordinary W1 -> W2 week reports no coffee change, and the same week with one served W1 price +1
    reports one removed and one added event `unexplained` (`tests/test_dq_rules.py`).
- It never fails a build (finding 2): `coffeedata build` copies the served files locally first (3 tries), a read
  that still fails is reported as `release not_reconciled unreadable: ...`, a compared column the served file
  lacks reads as null and is reported as `schema missing_column:<col>`, and the model and its tests run after the
  gate in a step of their own, whose failure is logged and recorded as `not_reconciled release_changes_missing`.

## Results

Wilson 95% intervals. "Main" is origin/main at 28a1b05; the test set was scored once, at bbe214e (rules and
predictions frozen there, `sealed/SHA256SUMS` checked first).

**#12 near-duplicate roasters** (main has no rule: it flags nothing)

| Set | Main recall | Change: flagged | Precision | Confidently wrong | Recall (labelled duplicates) |
|---|---|---|---|---|---|
| train | 0/20 | 20 | 1.000 (20/20; 0.839-1.000) | 0.000 (0/20; 0.000-0.161) | 1.000 (20/20; 0.839-1.000) |
| validation | 0/6 | 6 | 1.000 (6/6; 0.610-1.000) | 0.000 (0/6; 0.000-0.390) | 1.000 (6/6; 0.610-1.000) |
| test | 0/11 | 11 | 1.000 (11/11; 0.741-1.000) | 0.000 (0/11; 0.000-0.259) | 1.000 (11/11; 0.741-1.000) |

Stratum-weighted recall is 1.000 on every set: no S3 (random) pair was a duplicate.

**#7 price per 100 g outliers** (main: `price_per_100g_in_range` only re-checks dim_coffee's filter, so it flags
nothing). Labelled sample, then stratum-weighted:

| Set | Flags in set | Precision (sample) | Confidently wrong (sample) | Weighted precision | Weighted false-flag share | Weighted recall of wrong prices |
|---|---|---|---|---|---|---|
| train | 583 | 0.800 (36/45; 0.662-0.891) | 0.133 (6/45; 0.063-0.262) | 0.857 | 0.041 | 0.326 (of ~1,535) |
| validation | 166 | 0.884 (38/43; 0.755-0.949) | 0.093 (4/43; 0.037-0.216) | 0.957 | 0.031 | 0.594 (of ~267) |
| test | 217 | 0.778 (35/45; 0.637-0.875) | 0.200 (9/45; 0.109-0.338) | 0.845 | 0.132 | 0.200 (of ~917) |

The recall estimates lean on the 15 labels in the middle stratum (one wrong label there stands for ~300 coffees),
so they are rough; the wrong prices the rule misses are mostly non-coffee items at ordinary prices.

**#11 per-adapter volume bands** (cells judged against the earlier clean builds; see the limits above)

| Set | Main (`adapter_ok_share`, 0.05): flagged | Precision | Confidently wrong | Change: flagged | Precision | Confidently wrong | Change recall |
|---|---|---|---|---|---|---|---|
| train | 13 | 0.154 (2/13; 0.043-0.422) | 0.769 (10/13; 0.497-0.918) | 1 | 1.000 (1/1; 0.207-1.000) | 0.000 (0/1; 0.000-0.793) | 0.500 (1/2; 0.095-0.905) |
| validation | 12 | 0.167 (2/12; 0.047-0.448) | 0.833 (10/12; 0.552-0.953) | 2 | 1.000 (2/2; 0.342-1.000) | 0.000 (0/2; 0.000-0.658) | 1.000 (2/2; 0.342-1.000) |
| test | 13 | 0.154 (2/13; 0.043-0.422) | 0.769 (10/13; 0.497-0.918) | 2 | 1.000 (2/2; 0.342-1.000) | 0.000 (0/2; 0.000-0.658) | 1.000 (2/2; 0.342-1.000) |

Main catches the same collapses (recall 2/2 on every set) but most of its flags are runs that tried fewer
roasters. The train miss is WooCommerce on 11-02, whose attempted-ok share fell 0.09 there, under the 0.10 band.

**#4 reconciliation**: within a release, 0 disagreements on all five builds. Orphan price events (10 coffees, all
by design): main's relationships warning flags train 3/3, test 7/7, all confidently wrong (by design); the change
flags 0. Validation has no item. Release against release: one recorded pair (956abd -> 5060ad), 26,322 rows added, removed or changed,
0 unexplained.

## Severity

All four warn, and none blocks yet:
- **#12** is precise (37 of 37 labelled flags right; test 11/11, Wilson lower bound 0.74). But blocking would hold
  back every release until the registry is fixed, since the same 37 pairs were served all along, and a block needs
  more than one release's worth of evidence. Candidate to block on a *new* pair once a few weeks show no false one.
- **#7** has 1 in 5 (sample) of its test flags on real prices, and wrong prices in every week's catalog: it would
  block every release. Warn, as a review list.
- **#11** has 2 real collapses per set to judge, and its bands aren't active in production before 6 clean builds.
- **#4** has had nothing to flag in any recorded build, so its false alarms can't be measured yet; the release
  reconciliation was measured on one pair of releases.

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
python label.py <dupes|price|adapter|recon> <data>/<rule>_inputs.json <A|B|C> <out>.json [--only <ids>]   # then split: test ids to sealed/
python dupes_eval.py   predict <data> '<rule>' <out>          # e.g. 'cont>=0.9,shared>=3,rcont>=0.3'; 'none'
python price_eval.py   predict <db> <data> '<rule>' <out>     # e.g. 'lo=3,hi=6,min_group=30'; 'none'
python adapter_eval.py predict <data> '<rule>' <out>          # e.g. 'ok_attempted:0.1+same_store:0.1+same_store_priced:0.1'
python <rule>_eval.py  score <data> labels <train|validation> <pred>... [--examples N]   # --examples: train only
```

Test scoring, once, totals and Wilson 95% intervals only (check `sealed/` first: `sha256sum -c SHA256SUMS`):

```
python dupes_eval.py   score <data> sealed test sealed/pred_dupes_main_test.json sealed/pred_dupes_change_test.json
python price_eval.py   score <data> sealed test sealed/pred_price_main_test.json sealed/pred_price_change_test.json
python adapter_eval.py score <data> sealed test sealed/pred_adapter_main_test.json sealed/pred_adapter_change_test.json
python recon_eval.py   score <data> sealed test
```

The label inputs carry dicts whose key order can differ between two runs of `cells`; the labels were made on the
first run's file.
