# Shopify ingestion architecture comparison: evidence

Collected 2026-10-31 to 2026-11-01 (UTC); archived 2026-11-01. These are the notes, results and analysis scripts
from comparing three designs for Shopify ingestion. It is a proof of concept and is not meant to be merged.

- Design write-up and POC: [#68](https://github.com/jtouley/coffee-data-tracker/pull/68) (draft)
- Per-store backoff and deferred retries, which all three designs build on: [#62](https://github.com/jtouley/coffee-data-tracker/pull/62) (draft)
- Secondary HTML crawler for refused roasters: [#67](https://github.com/jtouley/coffee-data-tracker/pull/67) (draft)

The code for each design is on its own branch: `archive/shopify-arch-a`, `archive/shopify-arch-b` and
`archive/shopify-arch-c`.

**How the real runs were made.** Each real crawl hit all 1,070 Shopify roasters in the registry. It ran from
this cloud container's **pooled (shared) egress**, not from Cloud Run's default egress, so the refusal rates
include whatever load other traffic put on those IPs. **Publishing was off** for every run: `HF_TOKEN` and
`HUGGING_FACE_HUB_TOKEN` were unset, `huggingface_hub` uploads were stubbed to raise, and the lakes were local.
Politeness stayed unchanged: robots.txt was respected, the crawler sent its own User-Agent, and per-host
spacing was kept. The confirmation run on Cloud Run default egress is still to come.

## arch-compare/

| File | What it is |
|---|---|
| `SCENARIO.md` | The shared simulated week every design ran: store mix, Shopify behaviour (S1 per-store, S2 plus an IP-wide limit) and the faults injected |
| `DECISIONS.md` | The owner's decisions carried into the design: Cloud Run Jobs + Scheduler, default egress, ELT, contracts unchanged |
| `PAGINATION.patch` | Pagination v1: limit=250, pages that can resume, page-level backoff |
| `PAGINATION.v2.patch` | Pagination v2: a conditional short-page stop, parking, quiet hosts, keeping body_text on parked pages |
| `<X>/NOTES.md` | The design summary for A, B or C: its shape, sim results, dbt equivalence, the real run, how it meets the principles, and its weaknesses |
| `<X>/result.json` | Sim results for that design: per scenario, per job and per day |
| `<X>/real.md` | Write-up of that design's real Shopify crawl: setup, numbers with intervals, and a verdict |
| `A/real2.md` | A's second real crawl, with pagination v1 + v2 and the burst fix |
| `B/retest.md` | B's targeted re-test of the page-2 stores and of the stores A was refused by |
| `<X>/*_responses.jsonl.gz` | One line per HTTP response in the real crawl (gzipped): time, host, path, page, status, Retry-After, and the job or phase. `status: null` means the limiter refused and nothing was sent. These hold no page bodies |
| `<X>/real/summary.json`, `A/real2/summary.json`, `B/real/retest_summary.json` | The metrics the analysis scripts computed from those responses |
| `<X>/real/*.py`, `A/real2/*.py` | The real-crawl drivers (`real_crawl*.py`, `realcrawl.py`, `retest.py`, `real_job.py`) and their analysis scripts (`analyze*.py`) |
| `<X>/sim/*.py` | The sim harness, mocks and report or analysis scripts (B's `dbt_equiv.py` is the dbt-equivalence check) |

## research/

| File | What it is |
|---|---|
| `shopify-503.md` | Desk research (docs, changelogs and forums only; no store was contacted) on Shopify storefront 429/503 waves and legitimate ways to get full catalogs |

## crawl-plan/

The earlier crawl-planning notes that the comparison builds on.

| File | What it is |
|---|---|
| `patterns.md` | Research on crawling through 429s: proxies, proven patterns and Shopify facts |
| `batch.md` | An evaluation and prototype of batch commits for the weekly crawl |
| `pools.md` | Per-adapter worker pools: a prototype and its measurements |
| `backoff.md` | Per-host backoff with a deferred retry queue: a prototype and its measurements (it became #62) |
| `live.md` | The live 429 test (2026-10-31 01:44-02:04 UTC, pooled egress, publishing off), asking whether the limit is per store or IP-wide |
| `refusers.md` | Shopify stores that refused: which failed on which IP, ranked by catalog size |
| `pr_body.md` | The draft PR body for the backoff POC |

## Left out

- The lakes (`*/real/lake`, `B/real/retest_lake`, `B/real/frozen`), C's local bucket (`C/real/bucket`), the sim run outputs (`*/sim/runs*`, `B/runs*`) and `*/sim/profiles.json` (an input for the sim). These are large, regenerable data.
- Run logs, lock and `.done` markers, and the per-shard or job JSON beyond the summaries.
- Caches and venvs. No raw page bodies, no file over 5 MB and no credentials are included.
