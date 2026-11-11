# Bag weight (grams) evaluation

This evaluates the grams `dim_coffee` prices bags with: `price_per_100g` needs a bag's weight, and most coffees
without one have a price but no parsed weight (the lake review's finding N1). `main` uses the grams the crawler
stored: read from each option's own title (`models.parse_grams`), or, on Shopify, the variant's own `grams` field.
The change (`coffeedata.bag_weight`, run in dbt by `stg_variant_grams` over each coffee's latest version) reads the
stored text again at build time. The current rules (round 2, frozen at `a4baff0`):

1. **Every option's title is read again.** A hyphen may sit between number and unit (`1-lb-custom-whole-bean`), a
   `/` or `,` before the number (`Whole Bean/12oz`), fractions and mixed fractions are read as such (`1/2 lb` was
   stored as 907 g, `.5lb` as 5 lb, `3/4 pound` as 4 lb; `1 1/3 lb` is 605 g), and slug decimals too (`0-5-lb`,
   `2-2-pounds`). The weight is **what the option's price buys**: a pack with an explicit count is its total (`4 x 12
   oz`, `12oz - 3 Pack`, `Case of 6 - 10 oz. bags`, `48/2 oz` portion packs, `5lbs Case Pack (8)`), and a
   subscription option is one delivery. A title that reads as a pack without a count, a bundle (`+ Mug`), a drink
   (`Nitro Cold Brew 12oz Can`, `Cold Brew Concentrate`) or two ways (`4/5 lb`) blanks the option, and so does a
   stored weight that came from that title but doesn't survive these rules.
2. **Options still without a weight**, when none has one of its own and they cost the same (within 15%), get the one
   weight the product title states, else the tags' one size, else the description's, else the URL handle's. Tags,
   the description and the handle are read only when the title states no weight, and the handle only when its words
   are the title's (stores copy pages). Nothing fills an option step 1 blanked on purpose (review H1).
3. **Every other stored weight is re-checked.** A stored weight the listing never states (Shopify's variant grams,
   often a shipping weight) is replaced by the weight step 2 finds, else kept only if the listing states that same
   weight anywhere, else dropped.

Each rule abstains rather than guesses: disagreeing weights, packs and bundles without a count, samplers, pods,
drinks, lot sizes, shipping terms and descriptions that offer several sizes leave the weight blank.

**The trade-off in step 3.** Dropping stored weights the listing never states is what lifts accuracy past 95% on
validation, and it costs coverage where Shopify's grams were the only source: on train, Shopify's share of coffees
with a `price_per_100g` goes from 94.2% (`main`) to 87.9%, and the share over every platform from 79.0% (`main`)
and 87.6% (round 1) to 81.1%. Labelers mark those listings `unknown`, so the eval scores keeping such a weight as
confidently wrong; whether a store's own grams field should count as the listing stating a weight is a product
decision this eval can't settle. On that build, current available retail coffees with a `price_per_100g` go from
72.9% (`main`) to 73.9% (round 1: 79.7%): WooCommerce 30.7% to 55.8%, Squarespace 49.1% to 75.8%, Square Online
6.4% to 11.3%, generic 18.1% to 26.8%, and Shopify 94.4% to 87.8%. `product_grams(..., unsupported="standard")` keeps such a weight when it is a
standard bag size (train 86.3% coverage, validation 88.6% accuracy / 8.2% wrong); `"keep"` keeps them all (87.7%,
87.5% / 10.2%).

**Running total (CLAUDE.md step 9):** round 1's sealed score reset the total to 0. Round 2 changes the output for
far more than 50 products: in a build of the stored lake (runs to 2026-11-09) against `main`, `price_per_100g` changes for 6,668 coffees (2,651 filled, 1,981 blanked, 2,036 changed) and `bag_sizes_g` for 10,175, so it gets the full process again: tuned on train, chosen on validation
totals, and a fresh sealed test set (`test2`). Once `test2` is scored, the total since the last full evaluation is
0; add each later change's count here.

## Data

- **Lake:** `hf://datasets/jtouley/coffee-data` at revision `f52838ec01c4002f0f84f994d23484eb8f797203` (head on
  2026-11-10), copied read-only into a scratch folder with `hive_partitioning = false` on every read. Nothing was
  written to the remote. The copy and everything derived from it (`population.json`, `pred_<name>.json` for the whole
  population, the labeling packets) hold listing text and stay out of git. The labeled items' text is in the
  regression fixture (`tests/fixtures/bag_grams_labels.jsonl`), as the classifier's is.
- **Population** (`bag_eval.py build`): every current retail coffee with an available price in the 2026-11-02
  snapshot, the run of the served release `20261104T041920-5060ad`: listed in its `public_coffee`, last seen 11-02,
  `catalog_status` available or preorder, not wholesale, and at least one available option priced above 0. 30,022
  coffees. Coffees with no available price (Square Online's price ranges, WooCommerce's #87) aren't a weight question
  and are left out. One option per coffee is the labeled item: the lowest `sha256(SALT:coffee_id:variant_id)` among
  its available, priced options. `population.json` is sorted by `coffee_id`, so every build gives the same file;
  its sha256 is recorded under "Sealed files".
- **Review L6:** the 11-02 snapshot partition is one batch, and `population.json` has no repeated `coffee_id`
  (checked: 30,022 rows, 30,022 ids). The price is the store's own (`price_usd` as stored): 31 rows of the served
  release come from five non-US stores (the lake review's finding 2), which `dim_coffee`'s USD filter drops. A
  weight's correctness doesn't depend on the currency, so they stay in.

## Split

- Rule: `n = int(sha256("coffeedata-bag-grams-eval-2026-11-10:" + group)[:8], 16) % 100`; `n < 60` is train,
  `n < 80` validation, the rest test. Salt: `coffeedata-bag-grams-eval-2026-11-10`.
- `group` is the store, not the roaster id: the resolved host from the release's `dim_roaster`, lower-cased, with a
  leading `www`, `www2`, `shop` or `store` removed (the lake review's duplicate roaster ids, e.g. Jones's three ids
  on one host, share a side). A roaster without a host uses its `roaster_id`.
- Made right after the shape inspection, before any rule was written or any example was read.
- Result: train 17,464 coffees (1,000 stores), validation 6,830 (356), test 5,728 (335).
- **The second test set (`test2`)** is drawn from the first test split's stores (the only stores none of whose
  listings was read while tuning, in either round), leaving out every item labeled in round 1. Salt:
  `coffeedata-bag-grams-test2-2026-11-11` (the per-stratum shuffle, `random.Random(SALT2:test2)`).

## Protocol

1. **Train:** rules are written against train examples only; in round 2, against the train labels' failures.
2. **Validation:** versions are compared on totals only, never example by example.
3. **Test (sealed):** the rules are frozen and committed before a test set is drawn. Its items and both rules'
   predictions are pinned in `SHA256SUMS` before any label exists, and it is scored once with `--final`. After its
   score a test set joins the regression fixtures and the next change draws a fresh one.
4. **Reference labels:** the true grams the labeled option's price buys, or `unknown` when the listing doesn't say,
   by `GUIDE.md`. Each item is labeled twice, independently, and a third labeler settles disagreements (`agree`,
   `settle`). The owner's orchestrator arranges the labelers. None of the rules' authors labels.
5. **Strata** (`stratum`, per item): **F** main has no grams and the change fills them; **C** main has grams and the
   change replaces or blanks them; **A** neither has grams; **K** both have the same grams. Drawn by a seeded
   shuffle per stratum, each weighted by its size in the set.
6. **Scores** (`score`, totals only): per rule and stratum, `right` (grams within 2% of the label), `wrong`
   (grams for an `unknown` label, or off by more than 2%: confidently wrong), `abstain_ok` (blank, label
   `unknown`) and `missed` (blank, but the listing states a weight), with Wilson 95% intervals. The stratum-weighted
   accuracy (`right` + `abstain_ok`), wrong share and right share, and the paired difference change minus main, are
   given with two 95% intervals: a normal approximation that takes items as independent, and a store-clustered
   stratified bootstrap (stores resampled within each stratum, 2,000 draws, fixed seed), which is the one to read
   when a stratum's items come from few stores (review M1, M2). `score` also prints how many stores each stratum's
   items come from.
7. **Ships if** the change's weighted test accuracy is at least main's and its weighted wrong share doesn't rise
   meaningfully (the paired wrong difference's interval should sit at or below about +1 point).
8. **Floors** (CLAUDE.md step 8): `tests/test_bag_grams_accuracy.py` scores the current rules on every labeled set
   in `tests/fixtures/bag_grams_labels.jsonl` (train, validation, and the first test set as a regression fixture),
   weighted by stratum, and fails if accuracy falls below or the wrong share rises above the measured level.
   `bag_eval.py fixture` writes the fixture (a description is cut after its last weight line; the predictions on
   the cut text equal those on the full text for every item).

## Round 1 (rules frozen at `e46a730`)

### Samples

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

### Results (scored 2026-11-11)

Labelled by Claude subagents (claude-opus-5-5): labelers A and B independently, blind to each other and to both
rules' predictions; a third (C), blind to A and B, settled the disagreements. A/B agreement (same word, or grams
within 2%): train 245/250, validation 374/375, test 489/500; 17 settled by C. The test set was scored once with
`--final`, after `sha256sum -c SHA256SUMS` passed.

Stratum-weighted. The first interval is the normal approximation (items independent; the table first published
here called these Wilson, which they aren't: Wilson intervals are per stratum, in `score`'s output); the second is
store-clustered.

| Set | Rule | Accuracy | Confidently wrong | Right (a weight) |
| --- | --- | --- | --- | --- |
| train | main | 82.7% [76.7, 88.8] [75.9, 88.1] | 7.5% [1.5, 13.5] [2.7, 14.2] | 72.7% [66.7, 78.7] [66.0, 77.5] |
| train | branch | 92.3% [86.2, 98.3] [85.4, 97.7] | 6.3% [0.3, 12.3] [1.5, 12.9] | 82.2% [76.2, 88.2] [75.5, 87.1] |
| validation | main | 78.4% [71.7, 85.1] [70.2, 86.0] | 14.1% [7.5, 20.6] [6.6, 22.0] | 63.0% [56.5, 69.6] [55.1, 70.5] |
| validation | branch | 84.1% [77.5, 90.8] [75.9, 91.8] | 13.2% [6.7, 19.8] [5.8, 21.2] | 68.8% [62.2, 75.4] [60.8, 76.3] |
| **test** | main | 78.7% [74.2, 83.2] [73.0, 83.6] | 11.6% [7.2, 15.9] [7.1, 16.9] | 66.1% [61.7, 70.4] [60.7, 70.5] |
| **test** | **branch** | **89.0% [84.4, 93.5] [83.0, 93.9]** | **7.6% [3.3, 12.0] [3.3, 13.0]** | **76.4% [72.0, 80.7] [70.7, 80.7]** |

Paired, branch minus main, on test: accuracy +10.3 points (normal [+9.9, +10.6], store-clustered [+8.9, +10.7]);
confidently wrong -3.9 points ([-4.1, -3.7], [-4.2, -3.4]). The change shipped on these numbers. Test stratum C is
concentrated: its 100 items come from 12 stores, 54 of them from one (greencastlecoffee.com), and most of the drop
in confidently wrong answers comes from C (main wrong on 99 of 100, the branch on 0); the clustered interval holds
the conclusion. Validation's C behaves differently: 75 items from 15 stores, the branch blank on 55 (packs it then
left blank). Two limits: in stratum F (weights the change fills) 9 of 199 test fills are wrong; and in stratum K
(weights both rules keep) 10 of 100 are wrong for both, so stored weights the change didn't touch still carried
errors. Round 2 addresses both.

## Round 2 (rules frozen at `a4baff0`)

The review of #136 (H1, M1–M5, L1–L6, nits) and the owner's levers: count-aware packs, re-checking every stored
weight, tags and the handle as sources, and the remaining confident misreads. Tuned on the train labels; chosen on
validation totals; the first test set's labels were not used for tuning. After the choice, the first test set was
scored as a regression fixture (totals only) to set its floor.

### What each lever bought

Stratum-weighted accuracy / confidently wrong; `coverage` is the share of train coffees with a `price_per_100g`.

| Version | Train | Validation | Train coverage |
|---|---|---|---|
| `main` | 82.7% / 7.5% | 78.4% / 14.1% | 79.0% |
| round 1 (`e46a730`) | 92.3% / 6.3% | 84.1% / 13.2% | 87.6% |
| + pack totals, H1, M4, M5, L1, L2, N1, portion packs, line-level descriptions; stored weights kept | 96.8% / 3.2% | 87.5% / 10.2% | 87.7% |
| + stored weights re-checked, unsupported ones kept if a standard size | 100.0% / 0.0% | 88.6% / 8.2% | 86.3% |
| + stored weights re-checked, unsupported ones dropped | 100.0% / 0.0% | 95.6% / 1.1% | 80.9% |
| + tags, then the handle (**chosen**, `branch2`) | 100.0% / 0.0% | 95.6% / 1.1% | 81.1% |

- Train reached 100% because the rules were written against its failures; validation is the honest number.
- Validation chose dropping unsupported stored weights over keeping standard sizes (95.6% / 1.1% against 88.6% /
  8.2%); train couldn't tell them apart.
- Tags and the handle change none of the labeled items in either set; they change 36 train products' grams (35 more
  with a `price_per_100g`), all read by hand on train (size tags such as `12oz`, handles such as
  `peru-cajamarca-12oz`). A first version read any handle and was wrong on copied pages (`italian-roast-12oz` on
  "French Roast", `copy-of-...`); the handle must now be the title's own.

### Scores of the frozen rules (`branch2`)

| Set | Rule | Accuracy | Confidently wrong | Paired accuracy | Paired wrong |
| --- | --- | --- | --- | --- | --- |
| train | main | 82.7% | 7.5% | | |
| train | branch2 | 100.0% | 0.0% | +17.3 [+11.9, +24.1] | -7.5 [-14.2, -2.7] |
| validation | main | 78.4% [70.2, 86.0] | 14.1% [6.6, 22.0] | | |
| validation | branch2 | **95.6% [91.9, 98.4]** | **1.1% [0.0, 3.5]** | +17.3 [+10.4, +24.9] | -13.0 [-20.9, -5.7] |
| first test (regression) | main | 78.7% [73.0, 83.6] | 11.6% [7.1, 16.9] | | |
| first test (regression) | branch2 | 92.6% [86.8, 97.0] | 1.6% [0.0, 4.0] | +13.9 [+9.7, +18.7] | -10.0 [-15.0, -5.5] |

Intervals are store-clustered. The first test set is no longer sealed: it was scored here after the rules were
chosen, and only its totals were read.

### Samples (`test2`, drawn after the freeze)

| | F | C | A | K | drawn |
|---|---|---|---|---|---|
| eligible (sizes) | 196 | 646 | 786 | 3,600 | |
| drawn | 196 | 100 | 100 | 100 | 496 |
| stores (largest) | 47 (20) | 36 (12) | 43 (12) | 71 (4) | |

Stratum F has 196 eligible items, so all are drawn. C is large because step 3 changes many stored Shopify weights.
`items_test2.json` also records each item's store, so `score` can cluster by it. The labeling packet
(`to_label_test2.json`, out of git) shows the listing with its tags and handle, every option and the option to
label, shuffled, with no stratum or prediction.

## Sealed files

`sha256sum -c SHA256SUMS`, run from this directory, checks the first test set (`items_test.json`,
`pred_main_test.json`, `pred_branch_test.json`) and the second (`items_test2.json`, `pred_main_test2.json`,
`pred_branch2_test2.json`). The population both were drawn from (out of git) was:

```
74fe323992a27a7c6cd2675dca81e5e131d75a6e7d1d12935f6920310794b273  population.json
```

Round 1's rules were frozen at commit `e46a730`, round 2's at `a4baff0`.

## Commands

```
python bag_eval.py build    <lake-dir> <data-dir>
PYTHONPATH=<main>/src   python bag_eval.py predict <data-dir> main      # main: the stored grams
PYTHONPATH=<branch>/src python bag_eval.py predict <data-dir> branch2   # coffeedata.bag_weight
PYTHONPATH=<branch>/src python bag_eval.py predict <data-dir> 'x+u=standard+s=title.body'   # a candidate
python bag_eval.py coverage <data-dir> <set> main branch2
python bag_eval.py sample   <data-dir> <set> main branch2      # writes items_<set>.json here
python bag_eval.py seal     <data-dir> <set> main branch2      # writes pred_<name>_<set>.json here
python bag_eval.py packet   <data-dir> <set>                   # the labelers' packet, in <data-dir>
python bag_eval.py fixture  <data-dir> ../../tests/fixtures/bag_grams_labels.jsonl train validation test
python bag_eval.py agree    <set> labels_A_<set>.json labels_B_<set>.json
python bag_eval.py settle   <set> labels_A_<set>.json labels_B_<set>.json labels_C_<set>.json labels_ref_<set>.json
python bag_eval.py score    <data-dir> <set> labels_ref_<set>.json main branch2 [--final for test2, once]
```

`score` refuses `test` and `test2` without `--final`, and refuses any rule whose predictions differ from its sealed
`pred_<name>_<set>.json`.

## What this doesn't cover

- Coffees with no available price: Square Online stores its listing's price only when the range is one price, and
  WooCommerce variation prices go null on a failed lookup (#87). Those need crawler work.
- The crawler still stores what `parse_grams` reads; the build reads the stored text again. Moving the same rules
  into the adapters, and storing what the adapters drop today (Shopify's option names, Square Online's item
  variations), is crawler work for after it's unfrozen.
- Review L4: `stg_products` and the classifier (`classify._bag_sized`) still read the stored grams, so a stored
  misread ("1/2 lb" as 907 g) still counts as bag-size evidence there. That keeps `is_coffee` unchanged by this
  change; moving the classifier onto these grams is a classifier change and is measured on its own eval.
- The first release built after merge records the changed prices as `attributes_changed` in `release_changes`
  (report-only).
