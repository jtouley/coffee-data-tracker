# Per-host backoff + deferred retry queue: prototype and measurement (worktree build/crawl-plan/wt-backoff, uncommitted)
**Measured** (sim/sim.py: 1,100 fake Shopify stores on 127.0.0.1, clock scaled 15x, 32 workers, 3 req/s, Shopify only; `ok` = run_ok_rate's catalog_ok share)
| scenario | main: Shopify pass | prototype | ok main / proto | global pauses main / proto |
|---|---|---|---|---|
| 4% of stores limit (2 req/60 s, then 429 + Retry-After) | 60.9 min | 22.1 min | 100% / 100% | 40 / 0 |
| 29% of stores limit (the share with a 429 on 10-30) | 160.2 min | 25.5 min | 100% / 100% | 145 / 0 |
| 4% + platform-wide 429 from min 5 to 9 | 66.7 min | 27.8 min | 99.9% / 100% | 46 / 4 |
**Real data** (lake fetches for 10-30, plus the job log): 118 of 404 Shopify stores (29%) got at least one 429, from 146 distinct hosts across 182 global pauses. Shopify was paused 77% of the time. 399 of 404 stores ended ok. 404 of ~1,100 stores were done when the 240-min timeout hit.
**Design.** (a) `PlatformLimiter(per_host=True)`: a 429/503 pauses only that host, until its Retry-After. A 430 (IP-wide), or blocks from >=5 hosts that make up >=50% of the stores that answered in the last 30 s, starts main's global pause and slowdown. (b) If a store's pause is >10 s it raises `HostRateLimited`. Rate limits and timeouts get parked. The failure is written at once as a normal `error` fetches row. Parked stores are retried after the main pass (`DEFERRED_ROUNDS=2`, with waits for Retry-After). The output reports `deferred: {round_1, still_failing}`. The weekly `--retry-failed` pass still runs after that.
**Size.** About 120 source lines (ratelimit +73, ingest +50, http +1) plus 7 unit tests (+105). 127 tests pass, including test_quality/test_dbt with dbt.
**Quality gate.** These floors are dbt tests, not quality.py: `run_ok_rate`, `run_is_complete` and `adapter_ok_rate`. Deferring doesn't change what counts as ok or complete. int_roaster_runs takes bool_or(ok) over the run's attempts, so a recovered store is ok. The error row from the first attempt counts it as attempted. stg_run_walk reads only ok rows. If the job dies during deferral, the store stays attempted and not-ok, which is correct. Rule: never hold back a failure row until the retry. A timeout would then turn the store into "not attempted" and fail run_is_complete (min_attempted_share 1.0).
**Risks.**
1. The 10-30 pattern may be an IP-wide bucket, not per-store limits: 429s every ~75 s even at 0.38 req/s, a median of 13.5 s after each pause ended, mostly different hosts. In that case 3 req/s brings more 429s. The 50% rule then trips and the run falls back to main's behaviour, so no faster.
2. The thresholds (5 hosts, 50%, 30 s, 10 s) come from the simulation, not real traffic.
3. In the burst scenario, 31 stores were parked before the detector tripped (burst 429s that weren't caught).
4. The `by_adapter` and `failures` summary counts an attempt each time, so its roaster count goes past the store count (the fetches data is unaffected).
5. The simulation leaves out the other ~1,450 non-Shopify stores sharing the workers, and real latency or timeouts. A 15x scale adds about 5% to times.
**Would it alone have saved last night?** Probably, if those 429s were per-store. The sim at the observed 29% rate goes from 160 to 26 min, and non-Shopify was done by 19:18, about 22 min in. It's unproven if the limit is IP-wide. Cheap test: a `--sample 200 --only shopify` run with the prototype, checking whether the 429 share rises at 3 req/s.
