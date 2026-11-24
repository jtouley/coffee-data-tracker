# Process-extraction evaluation

This evaluates `extract.rules()`'s `process` (washed, natural, honey, anaerobic, other) on the live catalog, run
against a local, read-only copy of the lake, following CLAUDE.md "Heuristics and models are measured, not argued".
The results are in `RESULTS.md`, the labelling definition in `GUIDE.md`.

**Running total (CLAUDE.md step 9):** 0 since this full evaluation (scored 2026-11-21). The change it measured
moved the rules' `process` of 2,215 products (1,938 listed; 487 of those in the sealed test set). Add each later
change's count here. The scored test set can now join the regression fixtures, and the next change draws a new one.

**Status (2026-11-21):** rules `r3` alone reach 94.5% ± 3.1 on the 300-item validation extension, short of the
owner's gate (at least 95% on a sealed test, confidently wrong no higher than main's). The owner chose rules first,
then Claude where they abstain (below); that combined field is scored once on sealed test 2 (400 labelled, unscored)
after a weekly enrichment run has read the sampled products the queue puts first. Hashes: `SHA256SUMS`,
`SHA256SUMS.round2`. Results: `RESULTS.md`.

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
   before any score (first the withdrawn `labels/A_test.json`, in commit `2751a76`; then A2's and B's; finally A, B, C and the merged reference, hashed in `d4f6fd9` before the one test score).

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
python process_eval.py settle  labels/A_<set>.json labels/B_<set>.json disagreements_<set>.json
python process_eval.py merge   labels/A_<set>.json labels/B_<set>.json labels/C_<set>.json labels/reference_<set>.json
python process_eval.py score   <data-dir> test labels/reference_test.json main branch --strata main,branch --final
```

`v5` is the train-tuned candidate the train and validation samples were drawn by (an uncommitted state between the
first rules and the freeze); its predictions are kept with the private data as `dev/process-eval-private/pred_v5.json`, so
the strata can be recomputed, and the samples are `dev/process-eval-private/sample_strata_*.json`. Check
the sealed files with `sha256sum -c SHA256SUMS`, run from this directory.

The test key list was first committed as `test_keys.txt` (hashed in `4e7e0a5`, before any test label) and moved to
`test_keys.json` the same day because the whitespace hook stripped the trailing space of one key; the keys are the
same 8,735, and `SHA256SUMS` now hashes the JSON file.

## Round 2: validation extension and the next sealed test

Drawn on 2026-11-21, after round 2's two candidates were committed (`3d9e49a`; `r2` is the committed rules,
`r2nc` the same with `extract.CONFLICT` off) and before any of their listings was opened.

- **Strata:** how main's and `r2`'s processes relate (`change(main, r2)`), over the listed catalog.
- **Order:** within a stratum, by `sha256("coffeedata-process-eval-2026-11-21:<tag>:" + key)`, tags
  `round2-validation` and `round2-test` (`process_eval.py draw`).
- **Excluded:** every key used before: the 100 train, 100 validation and 100 spent test listings, the 100
  diagnosis listings, and, for the test, the validation extension.
- **Validation extension** (`sample_validation2.json`, validation roasters only): same_none 120 of 5,280,
  same_process 120 of 1,343, none_to_process 30 of 472, process_to_none 15 of 117, process_to_other 15 of 80 (300).
  It is scored on its own, by its own strata, to choose between `r2` and `r2nc` on aggregates only.
- **Sealed test 2** (`sample_test2.json`, test-split roasters, the spent 100 left out): same_none 200 of 5,783,
  same_process 100 of 1,065, none_to_process 60 of 620, process_to_none 20 of 100, process_to_other 20 of 95
  (400). The rule author read the spent test listings as a (withdrawn) labeller; the new set shares their roasters
  but none of their listings, and the rule author has not opened any of the 400.
- **Hashed before any packet was built** (`SHA256SUMS.round2`): GUIDE.md (frozen; `9001240b…`), the test sample
  with its strata sizes, and main's, `r2`'s and `r2nc`'s predictions over the whole test split.
- **Packets:** one folder per labeller holding only `packet.txt` (the listings, shuffled, no prediction, label or
  stratum) and GUIDE.md: `proc-v2-A`, `proc-v2-S` (300 listings), `proc-t2-A`, `proc-t2-S` (400), and empty
  `proc-v2-H`, `proc-t2-H` for the settles. The test packet's sha256 is
  `fdc2424a34a50c87…` (both copies identical).
- **Scoring, once labelled:** `score <data> validation labels/reference_validation2.json main r2nc r2 --strata
  main,r2` (choose by aggregates), then for the frozen choice, once: `score <data> test
  labels/reference_test2.json main <choice> --strata main,r2 --final`.

## Round 3: train labels aimed at the abstentions

Drawn 2026-11-21 from the train split only (`sample_train3.json`), strata `change(main, r2)`, ordered within a
stratum by `sha256("coffeedata-process-eval-2026-11-21:round3-train:" + key)`, excluding every key used before
(train, validation, spent test, diagnosis, the validation extension and sealed test 2; 1,100 keys):
same_none 200 of 17,868, none_to_process 25 of 1,617, process_to_other 25 of 226 (250). These are for tuning (train):
their listings may be read once labelled. Packets: `proc-tr3-A`, `proc-tr3-S` (250 listings, GUIDE.md `9001240b…`,
packet sha256 `01c26df9…`), empty `proc-tr3-H` for the settles. Sealed test 2 is untouched.

## Rules, then Claude (the owner's choice, 2026-11-21)

The shipped field is the rules' process where they give one, else Claude's enrichment, else none.

- **How it worked before:** `stg_enrichments` keeps one row per (product, description), Claude's (`llm`) before the
  rules'. `dim_coffee` took every attribute from Claude's row when there was one, and that row is the rules' answer
  overwritten by any non-empty answer of Claude's (`extract.parse_llm_message`), so Claude overrode the rules'
  process. The weekly job (`enrich.py`) sends up to 2,000 coffees whose rules leave origin, process or notes empty
  and that Claude hasn't read, oldest snapshot first.
- **Precedence now:** `dim_coffee.process = coalesce(rules, Claude)`. Today's rules stand where they answer; Claude
  fills only where they abstain. Other attributes are unchanged.
- **Queue:** Claude's weekly cap goes first to current products (the product's latest version, in its roaster's
  latest run) that the rules read as a single origin with no process, and whose title, tags or description has a
  process word (`extract.process_first`, `PROCESS_HINT`). The rest keep the old order. The rule is catalog-wide.
- **The filter** (tuned on train only): of the labelled train listings that state a process the rules abstain on
  (6, after round 3), it catches 4; the other 2 are blends, which it leaves out by design. On the validation
  extension (aggregates only) it catches 6 of 7. Of 199 train listings labelled `none`, 30 pass it.
- **Volume** (lake `6ccb08b`): 5,819 listed single origins pass, 5,309 not yet read by Claude: about 2.7 weekly runs
  at 2,000. Reading them in one run would cost roughly $1.30 at the price `docs/app/PLAN.md` cites for the
  enrichment model's tier ($0.10 / $0.50 per million tokens; about 1,500 tokens in and 200 out a coffee), less
  through the Batches API. The batch size is the owner's call; unchanged here.
- **Scoring the combined field** (not run): after a real weekly run, with a fresh read-only lake copy and its
  predictions rebuilt, `python process_eval.py combined <data> <lake> r3 sample_test2.json combined` writes
  `pred_combined.json`, refusing while any sampled product the queue puts first has no Claude row for its current
  description (now: 44 put first, 4 read). Then, once: `score <data> test <reference_test2.json> main combined
  --strata main,r2 --final`. The test labels are committed with their hashes before that score.
