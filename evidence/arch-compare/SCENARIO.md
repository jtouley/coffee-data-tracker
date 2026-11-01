# Shared scenario for comparing ingestion designs A, B, C (local only)

All three designs run the SAME simulated week so results compare. No GCP, no real stores, no HF writes.

## Stores (from the roaster registry mix)
shopify 1,070 · generic 697 · squarespace 339 · woocommerce 233 · square_online 206 (about 2,545).
Reuse the localhost mock servers and harness in build/crawl-plan/sim/ (mockserver.py, profiles.py, harness.py, sim.py).

## Shopify behaviour (from the live test, build/crawl-plan/live.md)
- 6% of Shopify stores (fixed set, chosen by hash) refuse: 429 with Retry-After 60 on every request, at any rate.
- Scenario S1: per-store limits only (the case the live test saw).
- Scenario S2: S1 plus a shared IP-wide limit. Above 2 req/s of Shopify traffic sustained over 60 s, any Shopify store returns 429 (the case runner IPs might hit).
- Run both.

## Faults (both scenarios)
- On day 3, every WooCommerce host returns 503 for 2 hours (failure isolation).
- On day 5, one run is killed at 50% progress (resume and deadline behaviour).

## Time
- Simulate 7 days with a scaled clock.
- The job time limit is 240 min per job (the GitHub Actions limit; a Cloud Run job would allow more, but use 240 to be strict).

## Metrics to report (write result.json in your design folder)
- Per job: wall minutes, requests, 429s, ok-rate per connector.
- End of week:
  - share of roasters with a successful fetch in the last 7 days, overall and per connector;
  - number of `refused` stores;
  - stores never fetched.
- Whether every job finished inside its limit.
- What the weekly release would contain. dbt must be UNCHANGED: the design has to hand dbt the same raw-run shape it reads today.
- Code: lines changed in src/ (ingestion), and confirm 0 changes in dbt/, app/ and contracts/.
- Extensibility: the files, lines and config touch points needed to add a hypothetical new connector (e.g. "bigcartel") with its own rate limit.
- Operational surface: how many scheduled entries or jobs it needs, and what the owner has to watch.
