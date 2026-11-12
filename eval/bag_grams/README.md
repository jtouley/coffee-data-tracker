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

- Round 3 (bag counts, below): against `main` on the eval population, the candidates change the grams of 151–152
  coffees and the `price_per_100g` of 75–87 (the chosen rules: 152 and 87), so it goes through the full process
  again (`test3`). Once `test3` was scored the total was 0.
- Review of #139, H1 and L1–L3 (below): **15** coffees change against round 3's frozen rules (14 with another
  `price_per_100g`), within step 9's light path. The final review's F1 (below) adds 2 more. **Running total since
  the last full evaluation: 17.** Why the light path (the final review's F2, for the owner to accept): step 9's
  count is met, and although H1 changes how a rule decides, on this population the fix round makes no new confident
  answer (every changed output returns to `main`'s value, goes blank, or is the count times the stated bag), and
  every changed test-split coffee is already in a scored fixture, so no unseen coffee is left for a fresh test set.

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
   in `tests/fixtures/bag_grams_labels.jsonl` (train, validation, and both scored test sets as regression fixtures),
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

Round 1's rules were frozen at commit `e46a730`, round 2's at `a4baff0`, round 3's at `28314a0`. `SHA256SUMS` also
pins round 3's `test3` (`items_test3.json`, `pred_branch2_test3.json` for `main`'s rules, `pred_branch3_test3.json`).

## Commands

```
python bag_eval.py build    <lake-dir> <data-dir>
PYTHONPATH=<main>/src   python bag_eval.py predict <data-dir> main      # main: the stored grams
PYTHONPATH=<branch>/src python bag_eval.py predict <data-dir> branch2   # coffeedata.bag_weight
PYTHONPATH=<branch>/src python bag_eval.py predict <data-dir> 'x+u=standard+s=title.body'   # a candidate
python bag_eval.py coverage <data-dir> <set> main branch2
python bag_eval.py sample   <data-dir> <set> main branch2      # writes items_<set>.json here
python bag_eval.py changed  <data-dir> <set> main <candidate>...   # round 3's census sets (train-3, validation-3, test3)
python bag_eval.py seal     <data-dir> <set> main branch2      # writes pred_<name>_<set>.json here
python bag_eval.py packet   <data-dir> <set>                   # the labelers' packet, in <data-dir>
python bag_eval.py fixture  <data-dir> ../../tests/fixtures/bag_grams_labels.jsonl train validation test test2
python bag_eval.py agree    <set> labels_A_<set>.json labels_B_<set>.json
python bag_eval.py settle   <set> labels_A_<set>.json labels_B_<set>.json labels_C_<set>.json labels_ref_<set>.json
python bag_eval.py score    <data-dir> <set> labels_ref_<set>.json main branch2 [--final for test2, once]
```

`score` refuses `test`, `test2` and `test3` without `--final`, refuses any rule whose predictions differ from its
sealed `pred_<name>_<set>.json`, and, for those test sets, refuses a rule with no sealed file at all (review L4 of
#136).

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

## Round 2 sealed result (test2, 2026-11-11)

Rules frozen at `a4baff0` with the owner's choice to drop unstated Shopify variant grams. `test2` (496 items, salt
`coffeedata-bag-grams-test2-2026-11-11`) was labelled by Claude subagents (claude-opus-5-5): A and B independently and
blind to the rules' predictions, agreeing on 488/496; a third (C), blind to A and B, settled the other 8. Scored once
with `--final` after `sha256sum -c SHA256SUMS` passed. Stratum-weighted; store-clustered 95% intervals.

| Rule | Accuracy | Confidently wrong | Right (a weight) |
| --- | --- | --- | --- |
| main | 84.2% [82.1, 86.3] | 10.5% [8.7, 11.8] | 70.7% [69.4, 72.5] |
| **branch2** | **95.4% [92.7, 97.3]** | **0.9% [0.1, 2.1]** | **78.1% [75.6, 80.3]** |

Paired, branch2 minus main: accuracy +11.2 points [+7.5, +14.0]; confidently wrong -9.6 points [-11.4, -6.9]. The
change ships: above the 95% target on the sealed set, with confidently wrong weights cut from about 1 in 10 to about 1
in 100. The cost the owner accepted: fewer unit prices on Shopify (94.4% to 87.8% of current retail coffees), because
store-entered weights the listing never states are no longer trusted (the owner's decision, 2026-11-11). `test2` has
joined the regression fixtures as `test-2026-11-11`, with floor 0.953 and ceiling 0.0087 (its score of 95.36% and
0.86%, rounded toward passing); the next change draws a new test set.

## Round 3: counts written as bags

The final review of #136 (finding M1): a count written as "N bags" wasn't read, so a multi-bag price got one bag's
weight ("Two 12 oz bags", "2 Bags - 12 oz", "... 12oz Whole Beans ... x 2 Bags", "Buy The Whole Roast: (4) 1lb
bags" all gave one bag), which halves the weight and doubles `price_per_100g`. With it, the review's L1 (slug
decimals in a handle: `colombia-huila-0-5-lb` gave 2268), L2 (a stored `1,000g` was dropped) and L3 (a tag about
shipping, `Free shipping over 2 lb`, filled 907).

### The rules (`src/coffeedata/bag_weight.py`)

- A count of bags right before or after a weight multiplies it, as other counts do: `2 Bags - 12 oz`, `2 Bags X
  17.6oz`, `Two Bags (2.2 lb each)`, `Two 12 oz bags`, `(2) 12 oz. bags`, `12oz (2 bags)`, `12 oz / 2 Bags`. One bag
  (`1 Bag (12 oz)`) is a bag, and a count already read (`2x 12oz ... to select two bags`) isn't read twice.
- A count of bags elsewhere in a title or an option (`... 12oz Whole Beans (Made in Brooklyn since 1972) x 2 Bags`):
  lever `bag_counts`, `multiply` (the total) or `block` (abstain).
- An option that gives only a count (`1 bag`, `3 bags`, `Whole Bean / 5 bags`) is set aside: more than one bag is
  blank, never one bag's weight, and no longer blocks the listing's other options; one bag is read like any other
  option. Lever `count_fill`: on, such options take the count times the one bag weight the product states (`3 bags`
  on "Organic Colombian Ground -12oz" is 1020), when their prices per bag agree within 1.5x (`COUNT_PRICE`; stores
  discount larger counts); off, they stay blank. A count that is also a pack or a case (`3 Bag Bonus Pack`, `1 Case
  (12 Bags)`) still blocks.
- A description with a count of bags near its weight (`Two 12 oz bags of specialty grade coffee`) gives no weight,
  as with other counts. A description's weight outside a bag's range (`roughly 2,000 lbs`, `70-kilo (154 lbs)
  bags`) is a lot or a harvest and isn't read; without this, L2's thousands comma turned such a lot size into a
  mention that blanked the bag's own line.
- L1: a handle's slug decimal is read as a decimal (`0-5-lb` is 227). L2: a thousands comma is one number. L3: a tag
  with shipping or lot wording (`_LOT`) is skipped.

Tuned on train only: the train coffees each candidate changes were read one by one against GUIDE (`examples`-style
diffs). That reading found and fixed two faults before any label: L2's lot-size side effect above, and count-only
options that read as packs (`3 Bag Bonus Pack`) which, once they stopped blocking, let K-cup and cocoa options be
filled. No validation or test listing was read.

### Candidates and the choice rule (fixed before any label)

Four candidates, `bag_counts` x `count_fill`: `b3+c=multiply+f=0`, `b3+c=block+f=0`, `b3+c=multiply+f=1`,
`b3+c=block+f=1`. `main` is round 2's rules (`branch2`, unchanged since `a4baff0`).

| Coffees whose grams change | train | validation | test split |
|---|---|---|---|
| any candidate against `main` | 39 | 24 | 89 |
| `multiply` against `block` | 4 | **0** | 43 |
| `count_fill` on against off | 6 | 3 | 15 |

Validation can't separate `multiply` from `block`, and barely separates `count_fill`. So the rule, written before any
label: choose on validation totals (accuracy, then the confidently wrong share); where validation ties, choose on
train totals; where train ties too, the candidate that abstains (`block`, `count_fill` off). The sealed `test3` then
measures the chosen rules against `main` once.

The existing labels don't help: on round 1's train and validation items every candidate equals `main`, except 13
validation fills where all four agree with each other. Accuracy is unchanged on train (100.0%) and up 0.2 points on
validation (95.6% to 95.8%, wrong 1.1% both) for every candidate.

### Sample design: a census of the changed coffees

The change touches about 150 of 30,022 coffees, so a stratified sample of all coffees would hold almost none of them.
Each set is instead a census of its split's coffees whose grams any candidate changes (one stratum, `D`), and each
item labels one changed option: the coffee's round-1 labeled option if it changes, else the changed option with the
lowest `sha256("coffeedata-bag-grams-counts-2026-11-11:" + coffee_id + ":" + variant_id)` (`bag_eval.py changed`).
Outside `D` every candidate equals `main`, so the paired difference there is exactly 0 and the population figure is
`D`'s difference times `D`'s share of the split.

| Set | Changed coffees | Stores (largest) | Reuse a reference label | To label |
|---|---|---|---|---|
| `train-3` | 39 | 15 (11) | 0 | 39 |
| `validation-3` | 24 | 7 (17) | 13 (round 1's validation, same option) | 11 |
| `test3` (after the freeze) | 82 (`count_fill` off) or 83 (on) | 8 (23) | 0 | 82 or 83 |

`test3` is drawn from the test split (the only stores no round has read while tuning) once the rules are frozen and
committed, leaving out the 6 changed coffees round 1 or `test2` labeled. Its items and both rules' predictions go into
`SHA256SUMS` before any label, and it is scored once with `--final`, with Wilson intervals on `D` and store-clustered
ones (8 stores, one with 23 items: the clustered interval is the one to read). `items_train-3.json` and
`items_validation-3.json` are here; `label_from` marks an item whose reference label is reused, and `packet`, `agree`
and `settle` skip it or fill it in.

Labeling, as in rounds 1 and 2: labelers A and B independently and blind to each other and to every prediction, and a
third (C) blind to both, settling only their disagreements. Train and validation are labeled first, then the rules are
frozen and `test3` drawn and labeled by fresh labelers.

### Labels for train-3 and validation-3 (2026-11-11)

Labelled by Claude subagents (claude-haiku-5-5) against `GUIDE.md`: labelers A and B independently, each blind to
the other and to every rule's prediction, with only the guide and the packet; a third (C), blind to A and B, settled
the items they didn't agree on. None of them wrote the rules. A person's spot-check is still to come, as for the
earlier rounds.

| Set | A–B agreement | Wilson 95% | Settled by C | Reference labels |
|---|---|---|---|---|
| validation-3 | 11/11 | [74.1, 100.0] | 0 | 24 (13 reused from round 1's validation) |
| train-3 | 34/39 (87.2%) | [73.3, 94.4] | 5 | 39 |

- Train's agreement is well below round 2's test2 (488/496, 98.4% [96.8, 99.2]). Four of the five items are one
  store's listings (store group `coffeemasters.shop`), one listing shape the two labelers read differently; C gave
  all four 1020. Every candidate leaves those four blank, so they count as misses for all four and decide nothing
  between them.
- The fifth is a key error, not a disagreement: labeler A wrote its label under a coffee id that names no coffee
  (the packet's id with its last four hex digits changed), with the same value B gave the real item. It wasn't
  re-keyed by hand; `agree` counts it as missing and C labeled the item. Counting it as agreement, train is 35/39
  (89.7% [76.4, 95.9]). `settle` now works item by item, so a stray key can't stand in for an item (the earlier
  rounds' references reproduce byte for byte).

### Scores and the choice

Stratum `D` only (every candidate equals `main` outside it). Wilson intervals per outcome; the weighted figures with
store-clustered intervals.

| Set | Rule | Right | Wrong | Missed | Accuracy | Confidently wrong |
|---|---|---|---|---|---|---|
| validation-3 | main | 0/24 (and 2/24 rightly blank) | 2/24 [2.3, 25.8] | 20/24 | 8.3% [0.0, 57.1] | 8.3% [0.0, 57.1] |
| validation-3 | multiply, fill off | 21/24 [69.0, 95.7] | 2/24 | 1/24 | 87.5% [37.5, 98.2] | 8.3% [0.0, 57.1] |
| validation-3 | block, fill off | 21/24 | 2/24 | 1/24 | 87.5% | 8.3% |
| validation-3 | multiply, fill on | **22/24 [74.2, 97.7]** | 2/24 | 0/24 | **91.7% [42.9, 100.0]** | 8.3% [0.0, 57.1] |
| validation-3 | block, fill on | 22/24 | 2/24 | 0/24 | 91.7% | 8.3% |
| train-3 | main | 0/39 | 32/39 [67.3, 91.0] | 6/39 | 2.6% [0.0, 10.7] | 82.1% [52.6, 97.5] |
| train-3 | multiply, fill off | 24/39 | 0/39 [0.0, 9.0] | 14/39 | 64.1% | 0.0% |
| train-3 | block, fill off | 20/39 | 0/39 | 18/39 | 53.8% | 0.0% |
| train-3 | multiply, fill on | **28/39 [56.2, 83.5]** | 1/39 [0.5, 13.2] | 10/39 | **71.8% [36.0, 93.0]** | 2.6% [0.0, 10.7] |
| train-3 | block, fill on | 24/39 | 1/39 | 14/39 | 61.5% | 2.6% |

By the rule fixed before labeling:
- **`count_fill` on**: validation's accuracy is higher with it (22/24 against 21/24, one item), its wrong share the
  same.
- **`bag_counts="multiply"`**: validation ties (no validation coffee separates the two), and so does its wrong
  share; train decides: 71.8% against 61.5%, wrong 2.6% both. The four train items that separate them are one
  store's titles (`... x 2 Bags`, on which A and B agreed), and `multiply` is right on all four.
- Both choices rest on very few items and stores; the sealed `test3` is the measure.
- Train shows one confidently wrong answer that `count_fill` adds; validation shows none.

The frozen rules (`branch3`) are `product_grams`' defaults, and the floors in `tests/test_bag_grams_accuracy.py` now
include train-3 and validation-3 at these measured values; on the earlier sets they rise where the rules improved
(validation 95.8% / 1.05%, the first test set 93.4% / 0.87%, test2 95.3% / 0.74%).

### `test3`, drawn after the freeze

Drawn at `28314a0` with `bag_eval.py changed <data> test3 branch2 branch3`: every test-split coffee whose grams the
frozen rules change against `main` (`branch2`), less the 6 that round 1 or `test2` labeled. 83 coffees from 8 stores
(the largest has 23); on every item's option the two rules differ. Its items and both rules' predictions are in
`SHA256SUMS`, committed before any label, and scored once with `score ... test3 labels_ref_test3.json branch2
branch3 --final`.

### Round 3 sealed result (test3, 2026-11-11)

Labelled by fresh Claude subagents (claude-haiku-5-5): A and B independently, blind to each other and to the rules'
predictions, agreeing on 83/83 (Wilson [95.6, 100.0]; round 2's test2: 488/496), so the tiebreak (C) had nothing to
settle. Scored once with `--final` after `sha256sum -c SHA256SUMS` passed for all 9 files. 83 items, none unclear,
from 8 stores (the largest 23).

Frozen round-3 rules (`28314a0`), the sealed score:

| Rule | Right | Confidently wrong | Missed | Accuracy | Confidently wrong share |
|---|---|---|---|---|---|
| main (`branch2`) | 13/83 [9.4, 25.0] | 67/83 [71.0, 87.8] | 3/83 [1.2, 10.1] | 15.7%, normal [7.8, 23.5], store-clustered [0.0, 55.3] | 80.7%, normal [72.2, 89.3], store-clustered [38.7, 98.3] |
| **change (`branch3`)** | **56/83 [56.8, 76.6]** | **13/83 [9.4, 25.0]** | 14/83 [10.3, 26.3] | **67.5%, normal [57.3, 77.6], store-clustered [17.5, 95.9]** | **15.7%, normal [7.8, 23.5], store-clustered [0.0, 55.3]** |

The head of #139 (after the review fixes below), on `test3` as a regression fixture, not a second sealed score: right
**68/83 [72.3, 88.7]**, confidently wrong **0/83 [0.0, 4.4]**, missed 15/83 [11.3, 27.7].

Right, wrong and missed carry Wilson 95% intervals; no item was `unknown`, so none can be rightly blank. Paired,
change minus main: accuracy +51.8 points (normal [+35.6, +68.0], store-clustered [-33.3, +94.4]); confidently wrong
-65.1 points (normal [-81.0, -49.1], store-clustered [-97.9, +15.3]).

- **By store (totals only).** Main wrong, change right: 54 items in 5 stores. Main wrong, change blank: 14 in 2
  stores. Main right, change wrong: **13, of which 12 in one store (ironmulecoffee.com)** and 1 in another. So the
  change's 13 confidently wrong answers are new ones, and almost all of them are one store's listing shape.
- **The shipping rule (protocol 7).** On the point estimates and the normal intervals the change ships easily: accuracy
  far above main's, confidently wrong far below. The store-clustered intervals, which are the ones to read here, are
  wide with 8 stores: the accuracy difference's interval includes 0, and the wrong difference's upper end (+15.3)
  is above the "about +1 point" bar. The data can't rule out that, store by store, the change is no better. That is
  the owner's call, with the ironmulecoffee.com regression in view: fixing it is a new change, measured on a new test
  set, and its listings were not read here.
- **Whole population** (the measure of rounds 1 and 2: each coffee's own drawn option). Every coffee whose drawn
  option the change touches is in a census with a reference label (96 coffees: 30 train, 18 validation, 48 test,
  including 4 from round 1's or `test2`'s labels). Net, the change makes 54 more coffees right and 55 fewer
  confidently wrong of 30,022: **+0.18 points of accuracy and -0.18 points of confidently wrong** (exact on the
  labels, no sampling), on top of round 2's 95.4% / 0.9% (`test2`).
- `test3` has joined the regression fixtures as `test3`, with floor 0.674 and ceiling 0.1567 (56/83 and 13/83). The
  running total since the last full evaluation is 0; the next change draws a new test set. (After the review's H1
  fix, below: floor 0.819 and ceiling 0.0.)

## The full-population gate

The bar of CLAUDE.md step 7 for this field, as the owner set it on 2026-11-11: the estimated share of all current
coffees (the population above, N = 30,022) whose drawn option the rules get right or rightly leave blank must be at
least 95%. `tests/test_bag_grams_accuracy.py::test_full_population_accuracy_meets_the_gate` holds it, and
`bag_eval.py gate <data> <rule>...` prints it with intervals.

```
A = ( #right in D  +  (N - |D|) * a_U ) / N
```

- **D** (`gate_d.json`, ids only): the coffees whose drawn option a round's change gave other grams, each with a
  reference label for that option (round 3's census; `bag_eval.py gate-d <data> <main> <change> gate_d.json` adds a
  round's coffees and refuses one without such a label). Counted exactly, with no sampling error. Round 3: 96.
- **a_U**: accuracy on every other coffee, from `validation` and `test2`, the two sets stratified over their whole
  split that no rule was tuned on (train was), leaving out their items in D. Each is stratum-weighted by its stratum
  sizes; the two are combined by those sizes (6,830 and 5,228 coffees). Splits are by store hash, so either
  split's coffees stand for the whole population.
- The interval comes from a_U alone: normal over the strata, and store-clustered (stores resampled within strata).

| Rule | Full-population accuracy | Normal 95% | Store-clustered 95% | D right |
|---|---|---|---|---|
| main before #139 (`branch2`) | 95.37% | [93.55, 97.19] | [92.89, 97.20] | 10/96 |
| #139 as frozen in round 3 (`branch3`) | 95.55% | [93.73, 97.37] | [93.07, 97.38] | 64/96 |
| **#139 at its head (after the review fixes)** | **95.57%** | [93.75, 97.39] | [93.08, 97.40] | 69/96 |

The gate is on the estimate. Both lower bounds are below 95%: the data can't show that the population is above it,
only that the best estimate is. A later rules change adds its own census to D (a new test set covers its changed
coffees) and must keep A at or above 0.95; the slice sets' floors stay at their measured values.

## Review of #139: H1 and L1–L5 (step 9's light path)

The specialist review (H1): a count of bags next to a weight that is already the total was multiplied. `2 Bags (24
oz total)` gave 1361 g instead of 680, the 12 ironmulecoffee.com regressions in `test3`, and the published
`price_per_100g` fell 41% on those coffees. One general rule replaces it (`_bags_count` in `bag_weight.py`), not a
store exception. A count of bags next to a weight:

- with **total wording** (`total`, `in all`, `combined`, `altogether`, `packed in`, `ships in`, `split into`) gives
  the stated weight as the total: `2 Bags (24 oz total)` 680, `5 lb (packed in 5 bags)` 2268;
- with **tier wording** (`or more`, `and up`, `at least`, `minimum`, `2+ bags`) abstains: `5lb (4 bags or more)`;
- with **`each`**, or in a shape that names each bag's weight (`2 Bags - 12 oz`, `2 Bags X 17.6oz`, `Two 12 oz
  bags`, `12oz x 2 bags`, `12 oz / 2 bags` where `/` separates an option's size from its number of bags),
  multiplies;
- in a shape that can name either (`24 oz (2 bags)`, `1 kg - 2 bags`, `12oz (2 bags)`, `2 Bags (24 oz)`) abstains.
  This is stricter than round 3, which multiplied the last two; no coffee in the population has them.

Also: **L1**, one bag is no count, so `1 Bag - 12oz / 3 Months Prepaid`, `1 Bag 12oz + Mug` stay blocked and a
description's `1 bag of 12oz` reads 340. **L2**, a slug digit pair is a decimal only as stores write them (halves,
`2-2` for 2.2 lb): `2-1-lb` abstains in an option, and `kenya-2-1-lb-bags` reads as two 1 lb bags. **L3**, a
count-only option alone is compared with the other options' prices as one bag each, and with nothing to compare it
abstains. **L4**, an `unclear` label in D counts as not right instead of crashing. **L5**, the gate test takes
`POPULATION` and the formula (`full_population`, `is_ok`) from `bag_eval.py`.

**The 15 changed coffees, each checked by hand** (14 in the test split, every one in a scored fixture: `test3` or
`test2`; 1 in validation). Against `main`, the head changes 142 coffees (round 3's frozen rules: 152).

| Coffees | Option | Round 3 | Now | Label | Check |
|---|---|---|---|---|---|
| 12 at ironmulecoffee.com (12 in `test3`) | `2 Bags (24 oz total)` (30 options) | 1361 | 680 | 680 on all 12 | right |
| haobaobaocoffee.com (`test3`), H1 | `5lb (4 bags or more)` (3 options) | 9072 | blank | 2268 | wrong → missed; against `main` (2268), **right → missed** |
| galaxygirlcoffee.com (`test2`), L2 | `1-2-lb` at $14.45 (not its drawn option) | 544 | blank | none for this option (the 454 label is the drawn `1-lb` option's) | most likely half a pound, so 544 was wrong: wrong → blank, as on `main` (544) it was wrong too |
| pourcityroasters.com (validation), L2 | `1-4-lb ` at $5.50 | 635 | blank | none | 1.4 lb at $5.50 is implausible (a quarter pound is likelier): right to abstain |

Measured on the fixture with these rules (floors and ceilings only move toward passing more strictly):

| Set | Accuracy | Confidently wrong | Floor / ceiling |
|---|---|---|---|
| `test3` | 81.9% (68/83) | 0.0% (0/83) | 0.674 → **0.819** / 0.1567 → **0.0** |
| every other set | unchanged | unchanged | unchanged |

The gate (`bag_eval.py gate`): 95.57% (normal [93.75, 97.39], store-clustered [93.08, 97.40]); D 69 of 96 right.
`gate_d.json` keeps round 3's 96: with these rules 91 coffees' drawn options differ from `main`, all among the 96,
and every one of the 96 has a label, so counting all of them exactly stays correct.

**Labeler runs (review N3).** The labelers were spawned by the orchestrator on claude-haiku-5-5; this README's author
wrote the packets and prompts and never saw a transcript, so independence rests on the orchestrator's record. The
inputs, by sha256 prefix (the packets themselves hold listing text and stay out of git): GUIDE `dbf87200da6f55a0`;
train-3 packet `459cffe555e45eeb`, validation-3 `11af0c8e4a495066`, C's train-3 packet `d197fb1e1d86ae08`, test3
`abe0095a3af63ac3`; prompts A `16ac7a4601fcb87e`, B `92a4b27860a1836f`, C `ce15f826fc595cf1` (round 3, phase 1), A
`d4aef819ec8d9800`, B `306a7a9503cf9c19` (phase 2). test3's A and B files are byte-identical (all 83 values agree,
and both kept the packet's key order).

### The final review of #139: F1, and the shapes left latent

**F1** (round 3's own regression): bendixcoffee.com's `Bulk Box (100 bags $1.85/ea)` read no count, since a count
had at most two digits, and took the title's one 2 oz sample bag (57 g). A count of bags may now have three digits,
so the option joins its listing's other count-only boxes (20, 35, 50, 75 bags) in the count-fill check and gets 100
x 57 = 5700, as the 20-bag box gets 1140 (labeled 1134); a longer count blocks. 2 coffees change (both in `test3`,
whose labeled options, the `Large Box` and `Small Box`, are unchanged), 24 options 57 → 5700, checked by hand: the
price per bag ($1.85) agrees with the other boxes', and count times stated size is what GUIDE asks. Their
`price_per_100g` doesn't change (5700 g is outside its bag band); `bag_sizes_g` no longer publishes 57.

**Known latent shapes (F4–F6), 0 coffees today, left for a later change:**
- F4: any text with the word `bag` and a tier phrase anywhere blanks (`12 oz Bag + Free Shipping`, `12 oz Bag -
  Minimum roast`): abstentions, not wrong answers.
- F5: total wording is checked before `each` and matches shipping times and prices (`2 Bags - 12oz each, ships in
  2-3 days`, `Two 12 oz bags ($30 total)`, `2 Bags - 12 oz - packed in nitrogen` read 340, not 680), as on `main`.
- F6: a blend or grade number before `N oz bags` (`House Blend #2 12 oz bags`, `Blend No. 5 12oz bags`), `Lot 12
  Bags` anywhere in a title, a counted case over 25 kg in a description (`12 x 5 lb bags`), and a `2 bags` tag
  beside a size tag: read confidently wrong or blank.
