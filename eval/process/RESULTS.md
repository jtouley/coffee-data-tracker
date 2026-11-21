# Process extraction: results

Lake revision `6ccb08b` (runs to 2026-11-16). Main `7251e1c` against the change, frozen at `ba515d4` (no change to
`src/` or `tests/` since). Reference: labellers A (Opus) and B (Sonnet), disagreements settled by C (Haiku)
(README, Reference labels). The diagnosis below was read by the agent that wrote the rules and is not a labelled
reference.

## Why the chart's process is mostly "not stated"

The site's chart "Single-origin coffees in stock, by process" reads `dim_coffee.process`. On this lake copy its
"not stated" bar is 10,507 (the public chart showed 10,495 a few days later). Where the value comes from:
the rules (`extract.rules`: a `process:` tag, the title, a `Process:` field) for every coffee; Claude's enrichment
replaces the whole row for the coffees it has read (`stg_enrichments`, `dim_coffee`).

- **Enrichment status of the 10,507:** 9,754 (93%) were never sent to Claude: the enrichment job reads 2,000
  candidates a week (`--enrich-limit 2000`; three weekly runs so far, 6,000 rows, one more batch of 2,000 in flight)
  out of about 35,900 listed coffees the rules leave incomplete. 753 were read by Claude, which found no process.
  118 have a Claude process on an older description only (the description changed, so that row no longer applies).
- **Hand-read sample:** 100 of the 10,507 from train roasters, 20 per platform, by salted hash (`diag`):

| Bucket | Sample | Weighted by platform | Of 10,507 |
|---|---|---|---|
| (a) the listing never states a process | 70 | 64.7% ± 13.6 | ≈ 6,800 |
| (b) it states one, the rules miss it | 22 | 28.7% ± 13.1 | ≈ 3,000 |
| (c) Claude never ran (cross-cuts a, b, d) | 90 | | 9,754 counted |
| (d) not really single-origin (blend, flavoured, multi-lot) | 8 | 6.6% ± 6.3 | ≈ 690 |

  All 22 of (b) were never sent to Claude; of (a), 10 had been read by Claude, which agreed nothing was stated.
  The misses in (b): a labelled field the rules' one pattern doesn't read (`PROCESSING METHOD:`, `PROCESS Washed`
  with no colon, `Processed:`, "Wet hulled" without the hyphen) 7; a plain `Washed` tag 1; prose only ("processed
  using Kenya's traditional washed method", "natural processing", "carbonic maceration", "Washed.") 14. The change
  reads the first two kinds: on these 22 it gives 7 right and none wrong; prose stays with Claude (below).
- **What that leaves for rules:** at most about a quarter of the bar. Most of it is listings that never say, which
  only a better-written listing fixes, and the rest of the gap is the enrichment backlog.

## Scores (listed catalog, stratified estimate ± 95%, against the merged reference)

| Set | Rules | Agree | Confidently wrong | Coverage |
|---|---|---|---|---|
| Train (100 labelled of 23,137) | main | 85.5% ± 7.5 | 2.8% ± 1.9 | 13.0% ± 1.9 |
| | change | 95.1% ± 2.3 | 0.8% ± 1.4 | 21.3% ± 7.7 |
| | paired | +9.6 ± 7.8 | −2.1 ± 1.4 | +8.3 ± 7.7 |
| Validation (100 of 7,292) | main | 84.3% ± 7.5 | 5.1% ± 2.9 | 16.1% ± 2.9 |
| | change | 90.0% ± 7.6 | 2.9% ± 2.9 | 20.2% ± 3.4 |
| | paired | +5.6 ± 1.7 | −2.1 ± 0.1 | +4.2 ± 1.7 |
| **Test, sealed, scored once** (100 of 7,663) | main | 86.4% ± 10.4 | 1.8% ± 0.2 | 14.6% ± 0.2 |
| | change | 90.9% ± 10.4 | 1.0% ± 0.7 | 18.6% ± 0.7 |
| | paired | +4.5 ± 1.2 | −0.8 ± 0.7 | +3.9 ± 0.7 |

Train was tuned on, so its row is optimistic. Test: `score … test labels/reference_test.json main branch
--strata main,branch --final`, run once after the labels were hashed (`SHA256SUMS`). On CLAUDE.md's bar the
change meets only the "at least as good as main" half (agreement up, confidently wrong down). The owner's gate for
#252 is stricter: estimated agreement over the listed catalog of at least 95% on a sealed test, with confidently
wrong no higher than main's. The test estimate, 90.9% ± 10.4, does not meet it.

**Measurement caveat.** GUIDE.md rules 8 to 11 were written after the rule author had read this test set's listings
(as labeller of the withdrawn labels), so the definition the A/B/C labels follow was shaped with test knowledge. The
next sealed set is drawn only after GUIDE.md is frozen and hashed, and its listings are not opened by the rule
author.

Validation choice, rechecked against the reference: reading the description's prose (`v10`) scores agree 92.7%,
confidently wrong 3.7%, coverage 23.7%; paired against the change +2.7 ± 2.3, +0.8 ± 1.0, +3.5 ± 1.7. The same
aggregates as against the withdrawn labels (validation's one changed label sits where both rules abstain), so the
choice stands: prose is not read, precision first.

## Effect on the chart (this lake copy, rules only where Claude hasn't read the coffee)

| | not stated | washed | natural | honey | other | anaerobic |
|---|---|---|---|---|---|---|
| main | 10,507 | 2,343 | 1,374 | 493 | 106 | 263 |
| change | 9,883 | 2,809 | 1,404 | 414 | 338 | 238 |

Honey and anaerobic fall where they were wrong (Sumatra "semi-washed"/wet-hulled is other; co-ferments and thermal
shock without anaerobic are other; "Natural & Washed" blends have no single process).

## Round 2 (branch `process-precision-r2`, not sealed-tested)

Owner's gate: estimated agreement at least 95% on a sealed test, confidently wrong no higher than main's.

**Train error analysis** (the frozen change `ba515d4` against the train reference, 12 of 100 wrong): the rules
abstain where the listing states a process 10 (stated only in prose 8; a label format not parsed, "Process natural"
and "Processing Method Alma Negra Natural" with no colon, 2); a wrong process 2 (a prose "anaerobic" lost to a
"Process: Washed" field; a co-ferment then "thoroughly washed" read as other); a wrong reference label 0 found;
a `none`/`multiple` boundary case 0.

**Candidates** (tuned on train, compared on validation aggregates only; strata as before):

| Candidate | Validation agree | Confidently wrong | Coverage |
|---|---|---|---|
| main | 84.3% ± 7.5 | 5.1% ± 2.9 | 16.1% ± 2.9 |
| frozen change `ba515d4` | 90.0% ± 7.6 | 2.9% ± 2.9 | 20.2% ± 3.4 |
| + colon-less labels | 91.5% ± 7.5 | 2.9% ± 2.9 | 21.8% ± 3.3 |
| + strict prose (sentence about this coffee; guards for other coffees, blends, decaf, flavourings, general claims, questions, two countries) and anaerobic over a method | 93.5% ± 7.4 | 2.9% ± 2.9 | 23.7% ± 3.1 |
| + a description that contradicts a title or field: no answer (a tag loses to it) | 92.6% ± 7.5 | 2.0% ± 2.5 | 22.8% ± 3.4 |

Paired, the strict-prose candidate against the frozen change: agree +3.5 ± 1.7, confidently wrong +0.0, coverage
+3.5 ± 1.7. On train it scores 99.1% (tuned on).

**Why validation can't show 95% with margin:** about 71% of the listed catalog is the `same_none` stratum (both
rules abstain) and 19% `same_process`; each has 20 labelled listings. One `same_none` listing whose process is
stated somewhere the rules don't read costs 3.6 points of the estimate, and 3 of 20 `same_process` listings are
wrong for main and every candidate alike (2.8 points). Train shows no such errors left to learn from, and the
listings behind validation's can't be read. With 100 labels a set, the interval is about ±7.5 points, so even a
true 95% would not show a lower bound near 95%. The best validated figure is 93.5% (the strict-prose candidate).

### Validation extension (300 labelled; A Opus, B Sonnet agreeing on 298, C Haiku settling 2)

Strata `change(main, r2)`, scored on its own (`score … validation labels/reference_validation2.json main r2nc r2
--strata main,r2`):

| Rules | Agree | Confidently wrong | Coverage |
|---|---|---|---|
| main | 87.2% ± 3.1 | 2.7% ± 0.3 | 18.4% ± 0.3 |
| `r2nc` (conflicts not handled) | 94.4% ± 3.1 | 1.4% ± 0.9 | 24.7% ± 0.8 |
| `r2` (a contradiction gives no answer) | **94.5% ± 3.1** | **1.3% ± 0.8** | 24.7% ± 0.8 |

Paired: `r2` − main +7.2 ± 1.5, −1.4 ± 0.9, +6.3 ± 0.9; `r2` − `r2nc` +0.1 ± 0.2, −0.1 ± 0.2, 0.0. Choice: `r2`.
It does not clear 95% with margin (lower bound 91.4%), so it is not frozen and the sealed test is not scored. By
stratum, the loss is mostly `same_none` (both rules abstain, 72% of the catalog): 113 of 120 right, the other 7
listings state a process the rules don't read (about 4.2 points); then `process_to_other` 11 of 15 and
`none_to_process` 26 of 30 (4 confidently wrong each).
