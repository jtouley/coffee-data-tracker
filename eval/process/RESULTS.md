# Process extraction: results

Lake revision `6ccb08b` (runs to 2026-11-16). Main `7251e1c` against the change `ba515d4`. Labels: labeller A only
(README, Reference labels), so every row here is provisional until B and the third labeller have labelled.

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

## Scores (listed catalog, stratified estimate ± 95%)

| Set | Rules | Agree | Confidently wrong | Coverage |
|---|---|---|---|---|
| Train (100 labelled of 23,137) | main | 85.9% ± 7.6 | 2.8% ± 1.9 | 13.0% ± 1.9 |
| | change | 95.5% ± 2.3 | 0.8% ± 1.4 | 21.3% ± 7.7 |
| | paired | +9.6 ± 7.8 | −2.1 ± 1.4 | +8.3 ± 7.7 |
| Validation (100 of 7,292) | main | 84.3% ± 7.5 | 5.1% ± 2.9 | 16.1% ± 2.9 |
| | change | 90.0% ± 7.6 | 2.9% ± 2.9 | 20.2% ± 3.4 |
| | paired | +5.6 ± 1.7 | −2.1 ± 0.1 | +4.2 ± 1.7 |
| Test (100 of 7,663) | | sealed, not scored | | |

Train was tuned on, so its row is optimistic. Per-stratum counts with Wilson intervals are printed by `score`.
On validation the stratum where main gave a process and the change gives none (108 products) was 20 of 20 right for
the change (main 0 of 20: blends with "Natural & Washed", Indonesian semi-washed read as honey); the change's
confidently wrong answers there and in `process_to_other` are 3 of 20 each.

Validation choice: the same rules with the description's prose read scored agree 92.7%, confidently wrong 3.7%,
coverage 23.7% (paired against the change: +2.7, +0.8, +3.5). Precision first: prose is not read.

## Effect on the chart (this lake copy, rules only where Claude hasn't read the coffee)

| | not stated | washed | natural | honey | other | anaerobic |
|---|---|---|---|---|---|---|
| main | 10,507 | 2,343 | 1,374 | 493 | 106 | 263 |
| change | 9,883 | 2,809 | 1,404 | 414 | 338 | 238 |

Honey and anaerobic fall where they were wrong (Sumatra "semi-washed"/wet-hulled is other; co-ferments and thermal
shock without anaerobic are other; "Natural & Washed" blends have no single process).
