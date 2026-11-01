# Design C, real condensed week: all 1,070 Shopify roasters (2026-10-31 20:25 to 11-01 01:34 UTC, this environment's pooled egress)
**Setup.** Worktree C/wt: main 6bda240, PR #62, PAGINATION.patch v1.1 and PAGINATION.v2.patch as of 19:22 UTC (limit=250, resumable pages, page backoffs of 60/120/240 s, parking, quiet_hosts, body_text kept on parked pages), plus Design C. Each job ran as its own process through `pipelines.run_connector(shard_k=k)`, `run_sweep` and `run_html_fallback` (C/real/real_job.py). Shopify ran at 1.5 req/s with 4 concurrent requests and 8 workers. Politeness was unchanged: robots.txt, the crawler's User-Agent and per-host spacing, with no disguise and no rotation. Publishing was off, checked before the first request: HF_TOKEN and HUGGING_FACE_HUB_TOKEN unset, huggingface_hub's HfApi and upload/download functions stubbed to raise (checked), and a local lake and bucket only (C/real/lake, C/real/bucket). The lock was taken atomically (noclobber) at 20:25:00, after B/real.done (20:05:48) and 20 minutes with the lock free. It was released at 01:34, and C/real.done was written. Every response is in C/real_responses.jsonl (job, ts, host, path, page, status, Retry-After). A `status: null` line is a limiter refusal that was never sent. Analysis: C/real/analyze.py, output in C/real/summary.json.

Shards 0 to 6 ran back to back with pauses of 2, 4, 8, 16, 32 and 32 min (the doubling was capped at 32). The sweep followed: waves at +0/15/60/120 min, each store only after its Retry-After, resuming from its parked page. Then refused stores went to the HTML-crawler stub, which logs them only.

| job | stores | wall | requests sent | 429s | hosts with a 429 | first catalog request 429 | ok | refused | parked | other error | inside 240 min |
|---|---|---|---|---|---|---|---|---|---|---|---|
| shard 0 | 152 | 12.8 min | 530 | 75 | 48 | 15.8% [10.8, 22.4] | 146 | 5 | 1 | 0 | yes |
| shard 1 (+2 min) | 143 | 12.4 min | 516 | 98 | 57 | 27.0% [20.3, 34.8] | 133 | 6 | 4 | 0 | yes |
| shard 2 (+4) | 154 | 12.7 min | 537 | 88 | 47 | 16.2% [11.2, 22.9] | 145 | 6 | 3 | 0 | yes |
| shard 3 (+8) | 151 | 11.5 min | 533 | 85 | 47 | 20.8% [15.1, 28.0] | 142 | 4 | 4 | 1 | yes |
| shard 4 (+16) | 157 | 13.0 min | 556 | 87 | 49 | 17.8% [12.6, 24.6] | 150 | 2 | 5 | 0 | yes |
| shard 5 (+32) | 159 | 11.9 min | 516 | 39 | 10 | 4.4% [2.1, 8.8] | 150 | 6 | 3 | 0 | yes |
| shard 6 (+32) | 154 | 12.2 min | 485 | 32 | 8 | 4.6% [2.2, 9.1] | 145 | 7 | 1 | 1 | yes |
| retry sweep | 57 parked | 128.3 min | 462 | 275 | 47 | n/a | 16 | 27 routed | 13 | 1 | yes |
| HTML stub | 27 refused | 0.3 s | 0 | 0 | 0 | n/a | 0 (stub) | 27 logged | | | yes |

**Sweep.** 57 parked candidates; **16 recovered**.
| wave | stores due | ok | routed as refused |
|---|---|---|---|
| +0 | 49 | 14 | 16 |
| +15 min | 8 | 0 | 0 |
| +60 min | 18 | 2 | 5 |
| +120 min | 8 | 0 | 6 |

**Totals.** 1,070 Shopify stores:
- ok: 1,027 = **96.0% [94.6, 97.0]**;
- refused, routed to the stub: 27;
- still parked after the sweep: 13;
- other errors: 3.

Requests sent: 4,135, of which 779 were 429s (18.8%), with no 430s and six 503s. Every job finished inside 240 min: the longest shard took 13.0 min and the sweep 128 min. The shards never went past 92 requests in any 60 s (their 1.5 req/s budget).

**429 pattern.**
- **Per store, as before.** Every Retry-After was 60, and robots.txt never got a 429 (0 of ~1,100). 266 of 1,058 hosts got a 429, and 242 of them later served a catalog page. 160 got a 429 on their first catalog request; 24 only ever got 429s. 321 of the 779 429s were on page 2 or later.
- **New this run: the first-contact rate fell after the long pauses.** On shards 0-4 (pauses up to 16 min) it was 16-27%; on shards 5 and 6 (after 32 min) it was 4.4% and 4.6%. The intervals don't overlap with shards 0-4, and shards are hash-random slices, so their makeup is comparable. This is confounded with time: the other designs' crawls ended at 20:05, and shards 5-6 ran 2.5-3 h later. So it points to a shared-egress budget that recovers within hours, not just to pause length.
- **False platform-wide pauses.** The burst detector tripped once in shards 0, 1, 2 and 4: 4 pauses of 60 s each, each halving the rate. It tripped 7 times in the sweep: its retries of known blockers counted as a platform-wide burst ("5 of 10 stores blocked within 30s"). That slowed the sweep to about 0.4 req/s. It is fixed in C/wt since this run: ingest.run(quiet_hosts=...) keeps sweep retries out of the detector. The test is tests/test_pipelines.py::test_a_sweeps_known_blockers_never_trip_the_platform_wide_pause.

**A's 11 page-2 stores: 6 of 11 now succeed.**
- In the shard: Death Wish and Red Bay.
- In the sweep, resumed at page 2: Colectivo and Cuvée.
- In the sweep, after refusing page 1 in their shard: Andytown and Winans.
- Still parked on page 2: Coffee AM and Four Barrel. This fresh local lake had no product counts, so the conditional short-page stop couldn't end them at page 1.
- Refused page 1 every time and routed to the stub: Counter Culture, Cat & Cloud and Groundwork.

**A's 22 refused stores in C:**
- 3 ok: Bird Rock, Temple, Terra Nova;
- 13 refused again;
- 6 parked: Alana's, Doma, High Bank, La Vela, Puroast, Toastique.

**Overlap of refused sets.**
- C's 27 vs A's 22: 13 in common.
- C's 27 vs B's 20: 9 in common.
- A and B: 5 in common.
- In all three: 4, namely chromaticcoffee.com, copperdoorcoffee.com, javabeanroast.com and ticoroasters.com.
- 9 of C's are refused by C only.

A stable hard core is small (4-13 stores). The rest move between runs, which again fits a per-store or shared-egress limit, not a fixed blocklist.

**Verdict.** The daily-shard shape held up live. Each shard took 12-13 min at 1.5 req/s with no IP-wide signal (0 430s). The sweep added 16 stores over 2 h. 96.0% matches A (96.6% [95.4, 97.6]) and B (96.3% [94.9, 97.2]) within the intervals, using a seventh of the traffic per job. The stub hook received the 27 refused stores. The refusal rate depends on recent egress load: spreading Shopify over the week (Design C's point) is what the shards 5-6 drop suggests, but this run can't separate pause length from time since the other crawls.
