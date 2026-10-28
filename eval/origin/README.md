# Origin-extraction evaluation

This evaluates `extract.rules()`'s `origin_country` on the live catalog, run against a local, read-only copy of
the lake. It follows the classifier's precedent (`tests/test_classifier_accuracy.py`, `eval/sealed/README.md`):
the products are split before any result is read, the rules are tuned on train, chosen on validation and scored
once on a sealed test set. The results are in `RESULTS.md`.

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

- Labellers:
  - All sets were labelled by Claude Opus 5.5 (`claude-opus-5-5`), the agent that wrote the rules, blind to
    predictions.
  - The sealed test set was labelled a second time, independently and blind, by Claude in the PR review. The two
    agreed on 82 of 82, so no third labeller was needed.
  - Both are Claude, so a person's spot-check is still worth doing.
- At 25 per stratum, per-stratum intervals are wide. The paired difference is tight because unchanged products
  contribute nothing to it.
- Hand-written probe listings (`tests/test_extract.py::test_origin_probes`) are regression tests, not evaluation.

## Sealing, next time

- Commit `pred_main_test.json`, `pred_branch_test.json` and `SHA256SUMS` in a commit of their own, and push it,
  before any test label exists. Commit the labels and the score afterwards.
  - In #41 the predictions, labels and hashes all arrived in one commit (3950ef9). Git couldn't show the order.
  - The review closed that gap by rebuilding the predictions exactly from the frozen code, and by a blind second
    label.
- Run pre-commit on the sealed files before hashing them, so a hook can't change them afterwards.
- The test set here was scored for #41 and is now spent. It may join the regression fixtures.
  - The next sealed evaluation of origin extraction needs a fresh test set: re-split with a new salt over products
    that weren't in these sets (for example, lots first seen after 2026-10-28), and record that salt here.
  - Follow-ups until then are measured on train and validation only.

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
