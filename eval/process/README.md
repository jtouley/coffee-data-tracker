# Process-extraction evaluation

This evaluates `extract.rules()`'s `process` (washed, natural, honey, anaerobic, other) on the live catalog, run
against a local, read-only copy of the lake, following CLAUDE.md "Heuristics and models are measured, not argued".
The results are in `RESULTS.md`, the labelling definition in `GUIDE.md`.

**Running total (CLAUDE.md step 9):** 0 since this full evaluation (scored 2026-11-21). The change it measured
moved the rules' `process` of 2,215 products (1,938 listed; 487 of those in the sealed test set). Add each later
change's count here. The scored test set can now join the regression fixtures, and the next change draws a new one.

**Status:** scored. Train, validation and the sealed test (once) against the merged reference; results in
`RESULTS.md`.

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
   (`ba515d4`) and `sample_test.json` were hashed before any test label existed, then the test labels were hashed
   before any score (first the withdrawn `labels/A_test.json`, in commit `2751a76`; now A2's and B's).

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
- Labellers: **A = Claude Opus, B = Claude Sonnet**, each a subagent run by the coordinating session in its own
  isolated folder holding only the packets (`packet_{set}.txt`) and the current GUIDE.md, blind to predictions,
  strata and each other. **C = Claude Haiku** settled the 4 items A and B disagreed on (validation 1, test 3), from
  a packet holding only GUIDE.md and those listings. `labels/{A,B,C}_{set}.json`; `merge` wrote
  `labels/reference_{set}.json` (A where A and B agree, else C). No labeller wrote the change (#254).
- Why different models: a second isolated Opus run matched A on 300 of 300, so two runs of one model are not two
  independent labellers; A and B are now different models.
- **Superseded:** the rule author's labels (`labels/A_*.json` until `bc57855`, withdrawn under #254) and the
  A2/B labels that replaced them (until `d17eb2a`: 299 of 300 agreement with each other and 297 to 299 with the
  withdrawn labels; their isolation was not established). Nothing is scored against either.
- GUIDE.md rules 8 to 11 (tags, the word "anaerobic", thermal shock, decaf components) were added in `d17eb2a`,
  before A, B and C labelled, to write down how the A2/B labellers had decided those cases; A, B and C labelled
  with them.
- The listings are private lake data. The packets are prepared under a `dev/process-eval/` prefix
  (`packet_{set}.txt`: GUIDE.md plus the shuffled listings; `settle/`), for the private dev dataset. The sample
  files, which group keys by stratum, and `pred_v5.json` are kept apart in `dev/process-eval-private/`
  (`sample_strata_{set}.json`), out of the labellers' folder: the stratum names would tell a labeller what the rules
  answered;
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
python process_eval.py score   <data-dir> train      labels/reference_train.json      main branch --strata main,v5
python process_eval.py score   <data-dir> validation labels/reference_validation.json main branch --strata main,v5
# per set: settle, the third labeller, merge; then score train and validation, and the test set once (totals only)
python process_eval.py settle  labels/A2_<set>.json labels/B_<set>.json disagreements_<set>.json
python process_eval.py merge   labels/A2_<set>.json labels/B_<set>.json labels/C_<set>.json labels/reference_<set>.json
python process_eval.py score   <data-dir> test labels/reference_test.json main branch --strata main,branch --final
```

`v5` is the train-tuned candidate the train and validation samples were drawn by (an uncommitted state between the
first rules and the freeze); its predictions are kept with the private data as `dev/process-eval-private/pred_v5.json`, so
the strata can be recomputed, and the samples are `dev/process-eval-private/sample_strata_*.json`. Check
the sealed files with `sha256sum -c SHA256SUMS`, run from this directory.

The test key list was first committed as `test_keys.txt` (hashed in `4e7e0a5`, before any test label) and moved to
`test_keys.json` the same day because the whitespace hook stripped the trailing space of one key; the keys are the
same 8,735, and `SHA256SUMS` now hashes the JSON file.
