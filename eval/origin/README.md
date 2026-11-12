# Origin-extraction evaluation

This evaluates `extract.rules()`'s `origin_country` on the live catalog, run against a local, read-only copy of
the lake. It follows the classifier's precedent (`tests/test_classifier_accuracy.py`, `eval/sealed/README.md`):
the products are split before any result is read, the rules are tuned on train, chosen on validation and scored
once on a sealed test set. The results are in `RESULTS.md`.

**Running total (CLAUDE.md step 9):** 26 products whose rules `origin_country` changed since the last full evaluation,
which is this one. Add each later change's count here.
- 0: decoding HTML entities in tags in dbt (`dbt-decode-tag-entities`) changed the tags of 1,087 products (1,090
  versions, 51 roasters) and the output of `origin_country` of none: built from main and from the change on a local
  copy of the lake (runs 2026-10-26 to 11-02, rules only), `stg_products` and every mart and public table were
  identical. 0 of them in the test set.
- 20: the ISO 3166-1 mapping (`eval/origin_iso`) fixed two aliases the rules apply before anything is stored
  (`origins.py`), compared with main 0879254 on one local copy of the lake (runs to 2026-11-09, stored verdicts):
  "New Guinea" had matched "Guinea", the West African country, and Puerto Rico was folded into the United States.
  Of the 20 coffees (34 product versions in `stg_products`): Guinea → New Guinea 12 (ISO: no single country, so
  no public origin), Guinea → Papua New Guinea 5 ("Papa"/"Papau"/"Paupau New Guinea", "Papua New Guinean"),
  United States → Puerto Rico 2, none → Papua New Guinea 1 ("Papa New Guinea Kongo"). Each was checked by hand
  against its title and description: all 20 are right after the change (one, a blend "Espresso" with "coffee from
  New Guinea", was and still is a blend's component, now without a country rather than with the wrong one). 5 of
  them are in the sealed test set (count only). Unit tests: `test_extract.py::test_new_guinea_and_puerto_rico_titles`
  and the alias cases.
- 6 more, same PR: "in <place>, <country>" no longer states an origin when the country goes on into a list ("our
  customers in New York, Puerto Rico, and El Paso"). Found through validation: one listing labelled "United States"
  (787 Coffee, a Puerto Rican roaster) read "Puerto Rico" after the alias fix. Two blind relabels (Claude Haiku,
  `claude-haiku-5-5`) both said Mexico ("Grown in Mexico"), so `labels_validation.json` now says Mexico; main had
  matched the old label only by reading the customers' cities. Two candidate list tests were compared on validation
  aggregates only: any comma, "&" or "and" after the country (98 of 111 agree, 9 wrong-country) against also
  requiring a capitalised next place (97, 10); the first is in. The 6 changed coffees, each checked by hand: 3 blends
  or tours listing origins ("from South America, Indonesia, and India") lose a single country, a Mexican artist's
  label ("in Tijuana, Mexico, …") no longer makes an Ethiopian coffee Mexican, a Villa Sarchi lot goes from Costa Rica
  (the variety's home) to Nicaragua, and one is in the sealed test set (count only; 6 of the 26 are).
  The floor (`tests/test_origin_accuracy.py`, same labels): main 0879254 96 of 111 agree, 12 wrong-country; this
  change 98 and 9; train unchanged (0.9596 / 0.0394). The validation floor is raised to 98 and 9.
  **Not a clean measure (review of #144):** this rule changes how the place-comma rule decides, and it was designed
  after a targeted relabel of the one validation listing that moved against the change, then compared on validation.
  So validation no longer measures it cleanly; the next sealed test set drawn for origin is its first clean measure.
  To make the validation figure honest, every validation listing whose prediction differs between main and the
  change (4, including the relabelled one) is being labelled again, blind, by two labelers and a tiebreak (packets
  hashed in `eval/origin_iso/SHA256SUMS`, round2/); the validation floor is then set from that result, never lowered.
  The rule's "and" is matched in any case since the review; on validation that changes nothing (98 / 9).

## Floor

`tests/test_origin_accuracy.py` holds the rules to this eval's train and validation labels (CLAUDE.md step 8): on train, stratum-weighted agreement at least 0.959 and wrong-country at most 0.040, which reproduces the train row of RESULTS.md (96.0% / 3.9%; strata in `floor_strata.json`, the two predictions the train sample was drawn from). Validation's sample can't be redrawn from any committed rule set (the closest shares 59 of its 111 keys), so its weights and RESULTS.md's 88.0% / 11.6% can't be reproduced; its floor is a count over the 111 fixed labelled listings instead: at least 98 agree, at most 9 wrong-country (raised by #144 after one label was corrected by two blind relabels; first set at 97 / 11 on main 0cfa4d9). The listings are private: `eval/floor_inputs.py` rebuilds them from the lake at df57f62, and the eval-floors workflow runs the floor (`docs/RULES.md`).

The floor's sensitivity is low: in train the `unchanged` stratum carries 97% of the weight on 25 labels, so one label there is worth about 3.9 points, and a regression that touches none of the labelled listings doesn't show. It catches a change on the labelled listings, not a change elsewhere.

## Data

- Source: `hf://datasets/jtouley/coffee-data`, downloaded with `huggingface_hub.snapshot_download(repo_type=
  "dataset", allow_patterns=["snapshots/*", "bodies/*", "enrichments/*", "verdicts/*", "marts/*", "registry/*"])`
  into a scratch folder. Nothing was ever written to the remote. The downloaded data and everything derived from
  it (`eval_set.parquet`, `split.json`, `pred_*.json` for train and validation) stay out of git.
- Eval set: every coffee in `marts/dim_coffee` (the current classifier's coffees: 44,293 products from 1,848
  roasters), with its latest snapshot (title, tags) and description (`bodies` joined on `body_hash`; 2,262 have none).
  Build it with `python origin_eval.py build <lake-dir> <data-dir>`.

## Split

- Rule: `n = int(sha256("coffeedata-origin-eval-2026-10-28:" + roaster_id)[:8], 16) % 100`. `n < 60` is train,
  `n < 80` is validation, and the rest is test. Salt: `coffeedata-origin-eval-2026-10-28`.
- The split is by roaster, so one roaster's lots never sit on both sides.
- It was made right after the dataset-shape inspection, before any prediction or changed example was read.
- Result: train 28,001 products (1,126 roasters), validation 7,879 (358), test 8,413 (364).
- `test_keys.txt` lists the test product keys.

## Protocol

1. **Train:** heuristics were tuned here. Examples and diffs were read only from this set
   (`compare … train … --examples`).
2. **Validation:** used to choose between candidate rule sets, from aggregate scores only.
3. **Test:** sealed. The rules were frozen at commit `9c2c98c` before the test set was sampled.
   - `pred_main_test.json` and `pred_branch_test.json` hold main's and the frozen branch's test predictions.
   - Both were written, and recorded in `SHA256SUMS`, before any test label existed.
   - The test listings were then labelled blind and scored once with `score … test … --final`.
4. **Reference:** the lake has no Claude extraction to compare against.
   - Every enrichment row is `method = 'rules'`, and `dim_coffee.enrichment_method` is `rules` for all 44,293 products.
   - So the reference is a hand-labelled sample instead.
   - Each set was stratified by how main's and the branch's origins relate (`sample`): up to 25 products per stratum,
     drawn by a seeded shuffle, without reading them.
   - The listings were shown shuffled and without any prediction (`show`), and each was labelled with the single
     country the listing says the coffee grew in, or `none` for a blend, a multi-origin pack, a non-coffee product or
     a listing that doesn't say.
   - The labels are in `labels_{train,validation,test}.json`.
5. **Scores:**
   - Agreement with the label, and the wrong-country rate: a country that isn't the label. An empty origin isn't
     wrong; `needs_llm()` sends it to Claude.
   - Both are given per stratum with Wilson 95% intervals.
   - The whole-set estimate weights each stratum by its size.
   - The paired difference is branch minus main on the same products, with a stratified 95% interval.

## Limits

- The labels are one labeller's. That is Claude (the agent that wrote the rules), not a human and not two
  independent labellers as in `eval/sealed`. It labelled blind to predictions, but a human pass over
  `labels_test.json` would make the reference stronger.
- At 25 per stratum, per-stratum intervals are wide. The paired difference is tight because unchanged products
  contribute nothing to it.
- Hand-written probe listings (`tests/test_extract.py::test_origin_probes`) are regression tests, not evaluation.

## Commands

```
python origin_eval.py build   <lake-dir> <data-dir>
python origin_eval.py split   <data-dir>
python origin_eval.py predict <data-dir> main      # with PYTHONPATH pointing at main's src
python origin_eval.py predict <data-dir> v2
python origin_eval.py compare <data-dir> train main v2 [--examples 30]
python origin_eval.py sample  <data-dir> <set> main v2 25
python origin_eval.py show    <data-dir> <data-dir>/to_label_<set>.json
python origin_eval.py score   <data-dir> <set> labels_<set>.json main v2 [--final for test]
```

Check the sealed files with `sha256sum -c SHA256SUMS`, run from this directory.

The pre-commit end-of-file hook later added a final newline to the JSON files. The hashes recorded before any test
label existed were of the files without it, and they still match with that newline removed
(`head -c -1 <file> | sha256sum`):

```
774eb8d65148355c31727c60fc13aded468246e5889605836e9fded2b4548e4f  pred_main_test.json
6e9923d457d8c91e1093190c6fc097d1c1b0792fc2dd4ea74b5b4ce9fbfccec7  pred_branch_test.json
3da6c9e0a0b9126db0127c190148a8ecee0c8a2f68fa22ded497f0f91c32fce5  labels_test.json   (hashed before scoring)
```
