**POC only: merges only after a live weekly-scale run proves it.**

## Problem
The weekly run on 2026-10-30 (Actions run 41111000693) timed out at 240 min. Shopify was paused for 77% of the run:
- `PlatformLimiter` paused all Shopify requests 182 times, each pause 60 s, and dropped to 0.38 req/s.
- Those 182 pauses came from 146 different hosts.
- The lake has fetches for 404 Shopify stores, out of about 1,100. 118 of them (29%) got at least one 429, and 399 still ended ok.

## Design
**Per-host backoff**, in `ratelimit.py`, with `PlatformLimiter(per_host=True)`:
- A 429 or 503 pauses only the store that sent it, until its Retry-After.
- The global pause and slowdown from `main` happen only on a 430 (Shopify's IP-wide block), or when at least `burst_hosts=5` stores block within `burst_window=30 s` and make up at least `burst_share=50%` of the stores that answered in that window.
- Deferred rounds never count towards a platform-wide pause. They retry only stores already known to block, so their 429s arrive together. The live test caught this as 6 false global pauses, down to 0.25 req/s.

**Deferred retry queue**, in `ingest.py`:
- A store asked to wait more than `defer_after=10 s` raises `HostRateLimited` instead of holding a worker. Those stores are parked, along with any other rate-limit failure (including 429s after the client's retries) and timeouts.
- After the main pass, parked stores get up to `DEFERRED_ROUNDS=2` more rounds. Each round waits out Retry-After and spreads its retries over `DEFERRED_SPREAD=60 s` rather than sending them all at once.
- A failure is written as a normal `error` fetches row, in the chunk it finishes in. Every chunk of a round is written before the next round starts, so a parked store's error row is in the lake before it is retried (tested). A run that dies during the deferred rounds therefore still shows the store as attempted for `run_is_complete`.
- A retry adds another row. `int_roaster_runs` counts a store ok if any of its attempts that run succeeded. `run_ok_rate`, `run_is_complete` and `adapter_ok_rate` don't change meaning.
- A store that got a block on every attempt that run (main pass plus the deferred rounds) and never served a catalog page ends with the error `refused: answered a block to every catalog request in this run (N attempts); last ...`. No new tables. Skipping such stores in later weeks is left for later.
- The ingest summary gains `deferred: {round_N, still_failing, refused}`.

## Simulation
1,100 fake Shopify stores on 127.0.0.1, clock scaled 15x, 32 workers, 3 req/s. Shopify only; no real stores are contacted. The prototype rows were re-run with this PR's code.

| scenario | `main` | this PR | ok `main` / PR | global pauses `main` / PR |
|---|---|---|---|---|
| 4% of stores limit (2 req/60 s, then 429 + Retry-After) | 60.9 min | 22.7 min | 100% / 100% | 40 / 0 |
| 29% of stores limit (the runner's 10-30 share) | 160.2 min | 25.5 min | 100% / 100% | 145 / 0 |
| 4% + platform-wide 429 from min 5 to 9 | 66.7 min | 27.8 min | 99.9% / 100% | 46 / 4 |

## Live
200 Shopify stores, publishing off, non-runner IP. 2026-10-31, run with the earlier prototype, before the deferred-round fix.

| phase | stores | reqs | 429 / reqs | stores with 429 | 430 | stores ok | wall (main pass) | global pauses |
|---|---|---|---|---|---|---|---|---|
| A, 1 req/s | 100 | 325 | 4.9% [3.1, 7.8] | 5.0% [2.2, 11.2] | 0 | 94% [87.5, 97.2] | 442 s (290 s) | 1 |
| B, 3 req/s | 100 | 318 | 9.4% [6.7, 13.1] | 8.0% [4.1, 15.0] | 0 | 92% [85.0, 95.9] | 442 s (95 s) | 3 |
| C, A's 429 stores ~20 min later | 5 | 35 | 60% [43.6, 74.4] | 5 of 5 | 0 | 2 more recovered | 333 s | 2 |
| runner, 10-30 (lake) | 404 | 1,360 | 10.7% [9.1, 12.4] | 29.2% [25.0, 33.8] | n/a | 98.8% [97.1, 99.5] | 240 min timeout | 182 |

Intervals are Wilson 95%. From this IP, 429s were per-store: a fixed 5-8% of stores refused their first `/products.json` whatever the rate, every Retry-After was 60, and there was no cross-host contagion. All 6 global pauses in A-C were the deferred-round false trips that this PR fixes.

## What's unproven
- **Runner IPs:** GitHub runners saw 29% of stores blocked, and different stores from the ones blocked in the live test. An IP-wide bucket on runner IPs isn't ruled out. If that's what it is, running at 3 req/s brings more 429s, the 50% rule trips, and the run falls back to `main`'s behaviour, so it gets no faster.
- **Thresholds:** `burst_hosts`, `burst_share`, `burst_window`, `defer_after` and `DEFERRED_SPREAD` are tuned on the simulation, not on real traffic.
- **Refusers:** they cost a few extra requests every week. With 5-8% of stores refusing from the live IP, the ok rate would be about 92-94%, close to `min_ok_share` 0.9.
- **Summary counts:** the `by_adapter` and `failures` summary counts an attempt each time, so a deferred store counts more than once. The fetches data and the gates aren't affected.

## Tests
- New tests in `tests/test_ratelimit.py`:
  - per-store pause;
  - burst by share;
  - spread-out blocks stay per-store;
  - 430 is global;
  - the deferred-round false trip, reproduced at both the limiter and the ingest level (both tests fail without the fix);
  - the deferred retries are spread out;
  - the failure row is written before the retry;
  - `refused` is recorded, and a store that served a page is not refused;
  - capped rounds.
- `test_persistent_429_is_a_fetch_error...` now expects the main pass plus 2 deferred rounds.
- Run: test_ratelimit, test_ingest, test_cli, test_catalog_failures, test_burst, test_adapters, test_quality and test_dbt (with dbt), plus pre-commit.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01S2czKYiXroCRE98BFBUay5
