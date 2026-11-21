# Process-extraction evaluation

This evaluates `extract.rules()`'s `process` (washed, natural, honey, anaerobic, other) on the live catalog, run
against a local, read-only copy of the lake, following CLAUDE.md "Heuristics and models are measured, not argued".
The results are in `RESULTS.md`, the labelling definition in `GUIDE.md`.

**Running total (CLAUDE.md step 9):** this is the first full evaluation of `process`. The change it measures moved
the rules' `process` of 2,215 products (1,938 of them in the listed catalog; 487 of those in the sealed test set,
count only). Add each later change's count here.

**Status:** train and validation are scored with one labeller's labels (labeller A). The sealed test set's
predictions, its sample and labeller A's test labels are hashed in `SHA256SUMS`; the test set is **not yet scored**,
because its reference needs labeller B and the third labeller first (Reference labels, below).

## Data

- Source: `hf://datasets/jtouley/coffee-data` at revision `6ccb08b6c8d133a2a1368e522b346f221968a011` (2026-11-18,
  runs to 2026-11-16), downloaded with `huggingface_hub.snapshot_download` (everything but `releases/`) into a
  read-only scratch folder. Nothing was written to the remote. The download and everything derived from it
  (`eval_set.parquet`, `split.json`, `pred_*.json`) stay out of git.
- Eval set: every coffee in `marts/dim_coffee` (43,390 products, 1,808 roasters) with its latest snapshot (title,
  tags, product type, variant titles) and description. Samples and scores use the **listed catalog** the site reads:
  `site.LISTED`, chains left out (38,092 products).

## Split

- Rule: `n = int(sha256("coffeedata-process-eval-2026-11-21:" + roaster_id)[:8], 16) % 100`; `n < 60` train,
  `n < 80` validation, the rest test. Salt: `coffeedata-process-eval-2026-11-21`. Grouped by roaster, so one
  roaster's products never sit on both sides.
- Made right after the shape inspection, before any prediction or example was read.
- Result: train 25,966 products (1,066 roasters; 23,137 listed), validation 8,689 (372; 7,292), test 8,735
  (370; 7,663). `test_keys.json` lists the test product keys.

## Protocol

1. **Diagnosis** (train only): 100 of the chart's "not stated" coffees (single-origin, listed, in stock, no process),
   20 per platform, first by a salted hash of the key, read by hand (`diag`). Counts in `RESULTS.md`.
2. **Train:** the rules were tuned on train examples only (`compare … train … --examples`, and the 100 labelled
   train listings).
3. **Validation:** two frozen candidates were compared on aggregates only: the rules with and without reading the
   description's prose ("processed using the washed method"). The prose reader added 3.5 points of coverage and
   0.8 of confidently wrong answers; on the listings it alone decided it was wrong about one time in five. Precision
   first: the candidate without prose is the change (commit `ba515d4`).
4. **Test (sealed):** sampled after the freeze; `pred_main_test.json` (main `7251e1c`) and `pred_branch_test.json`
   (`ba515d4`) and `sample_test.json` were hashed before any test label existed, then labeller A's test labels
   (`labels/A_test.json`) were hashed before any score.

Samples: per set, up to 20 listed products per stratum of how main's and the change's processes relate
(`same_none`, `same_process`, `none_to_process`, `process_to_none`, `process_to_other`), first by a salted hash.
Train and validation were drawn by main against the train-tuned candidate `v5`; test by main against the frozen
change. Whole-set figures weight each stratum by its size; intervals are Wilson 95% per stratum and a stratified
95% interval for the whole set and the paired difference.

Scores: **agree** (the label, or no process when the label is `none` or `multiple`), **confidently wrong** (a
process that isn't the label; an empty process is never wrong, `needs_llm()` sends it to Claude) and **coverage**
(the listing states one process and the rules give it).

## Reference labels

- Definition: `GUIDE.md`, written before any labelling.
- Labeller A: Claude (the agent that wrote the rules, Opus), blind to predictions, from the view `show` prints
  (title, tags, variant names, the first 500 characters of the description and every later passage with a process
  word; the vocabulary is much wider than the rules'). `labels/A_{train,validation,test}.json`.
- **Not done yet:** labeller B (independent, same packet, no access to A's labels or any prediction) and the third
  labeller for disagreements (a Sonnet subagent, the owner's standing instruction). The agent that built this had no
  subagent tool, so the blind packets are prepared instead (below). Until then train and validation are one
  labeller's, and the labeller wrote the rules: read them as provisional.
- The listings are private lake data. The packets are prepared under a `dev/process-eval/` prefix
  (`packet_{set}.txt`: GUIDE.md plus the shuffled listings; `to_label_{set}.json`), for the private dev dataset;
  they are not uploaded by this change.

## Commands

```
python process_eval.py build   <lake-dir> <data-dir>
python process_eval.py split   <data-dir>
python process_eval.py predict <data-dir> main        # with main's src importable
python process_eval.py predict <data-dir> branch
python process_eval.py diag    <data-dir> 20
python process_eval.py sample  <data-dir> <set> main <candidate> 20
python process_eval.py packet  <data-dir> <set> dev/process-eval/packet_<set>.txt
python process_eval.py score   <data-dir> train      labels/A_train.json      main branch --strata main,v5
python process_eval.py score   <data-dir> validation labels/A_validation.json main branch --strata main,v5
# after B and the third labeller: settle, merge, then score the test set once (prints totals only)
python process_eval.py settle  labels/A_test.json labels/B_test.json disagreements_test.json
python process_eval.py merge   labels/A_test.json labels/B_test.json labels/C_test.json labels/reference_test.json
python process_eval.py score   <data-dir> test labels/reference_test.json main branch --strata main,branch --final
```

`v5` is the train-tuned candidate the train and validation samples were drawn by (an uncommitted state between the
first rules and the freeze); its predictions are kept with the private data as `dev/process-eval/pred_v5.json`, so
the strata can be recomputed, and the samples are `dev/process-eval/to_label_*.json`. Check
the sealed files with `sha256sum -c SHA256SUMS`, run from this directory.

The test key list was first committed as `test_keys.txt` (hashed in `4e7e0a5`, before any test label) and moved to
`test_keys.json` the same day because the whitespace hook stripped the trailing space of one key; the keys are the
same 8,735, and `SHA256SUMS` now hashes the JSON file.
