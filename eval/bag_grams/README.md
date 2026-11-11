# Bag weight (grams) evaluation

This evaluates the grams `dim_coffee` prices bags with: `price_per_100g` needs a bag's weight, and most coffees
without one have a price but no parsed weight (the lake review's finding N1). `main` uses the grams the crawler
stored, read from each option's own title (`models.parse_grams`). The change (`coffeedata.bag_weight`, run in dbt by
`stg_variant_grams`) reads the stored text again at build time:

1. an option's title, with a hyphen allowed between number and unit (`1-lb-custom-whole-bean`), fractions read as
   fractions (`1/2 lb` was stored as 907 g, `.5lb` as 5 lb, `3/4 pound` as 4 lb), explicit counts multiplied
   (`2x1 lb`), and packs and cases left blank (`12oz - 3 Pack` was stored as 340 g). It replaces a stored weight
   only where that weight was read from the same title (not Shopify's variant grams), and keeps the stored value
   when the two agree to 2%;
2. when no option has a weight, the one weight the product title states, else the description's, for every option,
   only if the options cost the same (within 15%), so they differ by grind, not size.

Each rule abstains rather than guesses: disagreeing weights, packs, samplers, pods, drinks, lot sizes, shipping
terms and descriptions that offer several sizes all leave the weight blank, as before.

**Running total (CLAUDE.md step 9):** this is the field's first full evaluation; it is in progress (labels pending).
This change alters the output of 3,392 coffees' `price_per_100g` (2,548 filled, 49 blanked, 795 changed) and 4,842
coffees' `bag_sizes_g` in a build of the stored lake (runs to 2026-11-09), so it gets the full process. Once the
test set is scored, the total since the last full evaluation is 0; add each later change's count here.

## Data

- **Lake:** `hf://datasets/jtouley/coffee-data` at revision `f52838ec01c4002f0f84f994d23484eb8f797203` (head on
  2026-11-10), copied read-only into a scratch folder with `hive_partitioning = false` on every read. Nothing was
  written to the remote. The copy and everything derived from it (`population.json`, `pred_<name>.json` for the whole
  population, the labeling packets) hold listing text and stay out of git.
- **Population** (`bag_eval.py build`): every current retail coffee with an available price in the 2026-11-02
  snapshot, the run of the served release `20261104T041920-5060ad`: listed in its `public_coffee`, last seen 11-02,
  `catalog_status` available or preorder, not wholesale, and at least one available option priced above 0. 30,022
  coffees. Coffees with no available price (Square Online's price ranges, WooCommerce's #87) aren't a weight question
  and are left out. One option per coffee is the labeled item: the lowest `sha256(SALT:coffee_id:variant_id)` among
  its available, priced options. `population.json` is sorted by `coffee_id`, so every build gives the same file;
  its sha256 is recorded under "Sealed files".

## Split

- Rule: `n = int(sha256("coffeedata-bag-grams-eval-2026-11-10:" + group)[:8], 16) % 100`; `n < 60` is train,
  `n < 80` validation, the rest test. Salt: `coffeedata-bag-grams-eval-2026-11-10`.
- `group` is the store, not the roaster id: the resolved host from the release's `dim_roaster`, lower-cased, with a
  leading `www`, `www2`, `shop` or `store` removed (the lake review's duplicate roaster ids, e.g. Jones's three ids
  on one host, share a side). A roaster without a host uses its `roaster_id`.
- Made right after the shape inspection, before any rule was written or any example was read.
- Result: train 17,464 coffees (1,000 stores), validation 6,830 (356), test 5,728 (335).

## Protocol

1. **Train:** the rules were written against train examples only (`examples`, and the scratch scripts that printed
   train diffs, fills from descriptions and unusual weights). Train coverage of `price_per_100g` went from 79.0% to
   87.6%:

   | Platform (train) | coffees | main | change |
   |---|---|---|---|
   | Shopify | 12,936 | 94.2% | 97.1% |
   | Squarespace | 1,567 | 51.4% | 83.5% |
   | WooCommerce | 1,244 | 40.2% | 78.5% |
   | generic | 1,114 | 20.5% | 27.3% |
   | Square Online | 603 | 11.8% | 25.0% |
   | all | 17,464 | 79.0% | 87.6% |
2. **Validation:** three candidates were compared on totals only, with no examples read: the option-title rule
   alone, plus the product title, plus the description.

   | | main | options | + title | + description (chosen) |
   |---|---|---|---|---|
   | coffees with a `price_per_100g` | 75.5% | 77.4% | 78.4% | 81.1% |
   | masked check: agree / disagree | | | 924 / 11 | 1,150 / 11 |

   The masked check is label-free: on coffees whose every option has a weight read from its own title, the options'
   titles and weights are hidden and the product-title and description rules asked again. The description adds 226
   agreements and no disagreement, so the full rule was chosen. On train the 20 disagreements were all a stored
   weight being wrong (a 10 lb case stored as one 2.5 lb bag) once two description misreads were fixed (a "20-30
   pound ruck sack", "orders over 2 pounds").
3. **Test (sealed):** the rules were frozen and committed before the samples were drawn. `items_test.json`,
   `pred_main_test.json` and `pred_branch_test.json` were written and pinned in `SHA256SUMS` before any label
   existed. The test listings are not read while developing. Score it once, with `score ... test ... --final`.
4. **Reference labels:** the true grams the labeled option's price buys, or `unknown` when the listing doesn't say,
   by `GUIDE.md`. Each item is labeled twice, independently, and a third labeler settles disagreements (`agree`,
   `settle`). The owner's orchestrator arranges the labelers; who labeled (people, or Claude and which model) is
   recorded here when they are done. None of the rules' authors labels.
5. **Strata** (`stratum`, per item): **F** main has no grams and the change fills them; **C** main has grams and the
   change replaces or blanks them; **A** neither has grams; **K** both have the same grams. Drawn by a seeded
   shuffle per stratum (`random.Random(SALT:set)`), each weighted by its size in the set.
6. **Scores** (`score`, totals only): per rule and stratum, `right` (grams within 2% of the label), `wrong`
   (grams for an `unknown` label, or off by more than 2%: confidently wrong), `abstain_ok` (blank, label
   `unknown`) and `missed` (blank, but the listing states a weight), with Wilson 95% intervals; the stratum-weighted
   accuracy (`right` + `abstain_ok`), wrong share and right share with normal 95% intervals; and the paired
   difference, change minus main, on the same items. `unclear` labels are left out and counted.
7. **Ships if** the change's weighted test accuracy is at least main's and its weighted wrong share doesn't rise
   meaningfully (the paired wrong difference's interval should sit at or below about +1 point).

## Samples

| Set | F | C | A | K | drawn |
|---|---|---|---|---|---|
| train (stratum sizes) | 1,545 | 228 | 1,912 | 13,779 | |
| train (drawn) | 100 | 50 | 50 | 50 | 250 |
| validation (sizes) | 389 | 75 | 1,173 | 5,193 | |
| validation (drawn) | 150 | 75 | 75 | 75 | 375 |
| test (sizes) | 380 | 244 | 902 | 4,202 | |
| test (drawn) | 200 | 100 | 100 | 100 | 500 |

`items_<set>.json` lists each set's drawn `coffee_id`s with their stratum and the stratum sizes; the labeled option
is `item_variant_id` in the population (and `label_this_option` in the packet). `pred_main_<set>.json` and
`pred_branch_<set>.json` hold each rule's grams for those options.

## Sealed files

`sha256sum -c SHA256SUMS`, run from this directory, checks `items_test.json`, `pred_main_test.json` and
`pred_branch_test.json`. The population they were drawn from (out of git) was:

```
74fe323992a27a7c6cd2675dca81e5e131d75a6e7d1d12935f6920310794b273  population.json
```

Rules frozen at commit `e46a730`.

## Commands

```
python bag_eval.py build    <lake-dir> <data-dir>
PYTHONPATH=<main>/src   python bag_eval.py predict <data-dir> main     # main: the stored grams
PYTHONPATH=<branch>/src python bag_eval.py predict <data-dir> branch   # coffeedata.bag_weight
python bag_eval.py coverage <data-dir> <set> main branch
PYTHONPATH=<branch>/src python bag_eval.py masked <data-dir> validation branch
python bag_eval.py sample   <data-dir> <set> main branch       # writes items_<set>.json here
python bag_eval.py seal     <data-dir> <set> main branch       # writes pred_<name>_<set>.json here
python bag_eval.py packet   <data-dir> <set>                   # the labelers' packet, in <data-dir>
python bag_eval.py agree    <set> labels_A_<set>.json labels_B_<set>.json
python bag_eval.py settle   <set> labels_A_<set>.json labels_B_<set>.json labels_C_<set>.json labels_ref_<set>.json
python bag_eval.py score    <data-dir> <set> labels_ref_<set>.json main branch [--final for test, once]
```

`score` refuses the test set without `--final`, and refuses any rule whose predictions differ from its sealed
`pred_<name>_<set>.json`.

## What this doesn't cover

- Coffees with no available price: Square Online stores its listing's price only when the range is one price, and
  WooCommerce variation prices go null on a failed lookup (#87). Those need crawler work.
- The crawler still stores what `parse_grams` reads; the build reads the stored text again. Moving the same rules
  into the adapters, and storing what the adapters drop today (Shopify's option names, Square Online's item
  variations), is crawler work for after it's unfrozen.

## Results (2026-11-11)

Labelled by Claude subagents (claude-opus-5-5): labelers A and B independently, blind to each other and to both
rules' predictions; a third (C), blind to A and B, settled the disagreements. A/B agreement (same word, or grams
within 2%): train 245/250, validation 374/375, test 489/500; 17 settled by C. The test set was scored once with
`--final`, after `sha256sum -c SHA256SUMS` passed. Stratum-weighted, Wilson 95% intervals.

| Set | Rule | Accuracy | Confidently wrong | Right (a weight) |
| --- | --- | --- | --- | --- |
| train | main | 82.8% | 7.5% [1.5, 13.5] | 72.7% |
| train | branch | 92.3% [86.2, 98.3] | 6.3% [0.3, 12.3] | 82.2% |
| validation | main | 78.3% | 14.1% [7.5, 20.6] | 63.0% |
| validation | branch | 84.1% [77.5, 90.8] | 13.2% [6.7, 19.8] | 68.8% |
| **test** | main | 78.7% [74.2, 83.2] | 11.6% [7.2, 15.9] | 66.1% |
| **test** | **branch** | **89.0% [84.4, 93.5]** | **7.6% [3.3, 12.0]** | **76.4%** |

Paired, branch minus main, on test: accuracy +10.3 points [+9.9, +10.6]; confidently wrong -3.9 points
[-4.1, -3.7]. The change ships: it is more accurate than `main` on every set and its confidently wrong share is
lower on every set. Two limits: in stratum F (weights the change fills) 9 of 199 test fills are wrong; and in
stratum K (weights both rules keep) 10 of 100 are wrong for both, so stored weights the change doesn't touch still
carry errors (a follow-up, not this change).

**Running total (CLAUDE.md step 9):** this full evaluation resets the total to 0.
