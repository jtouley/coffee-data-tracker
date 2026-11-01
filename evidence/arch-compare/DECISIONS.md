# Owner decisions to carry into the design doc

- dbt, data models, contracts and app stay unchanged; only ingestion changes. Each design hands dbt today's raw-run shape.
- Run on Cloud Run Jobs plus Cloud Scheduler, not a self-hosted GitHub runner (#64 closed; a runner on a public repo isn't safe).
- No Prefect for now. Add it later only if the job list gets hard to follow; the jobs are containers, so it's a wrapper.
- Egress, Occam's razor: Cloud Run's DEFAULT egress (Google shared IPs). No NAT, no static IPs, no swap job.
  - Every run logs its observed egress IP(s) and the per-IP refusal rate (429/430 on first contact, per store) into the run manifest.
  - Regression check: an IP above 8% refusals on two runs in a row gets flagged and the owner is emailed. No automatic action.
  - Escalate to Cloud NAT with 1 IP, then N IPs, then a separate egress-swap job ONLY if refusals stay high in the data.
- Stores that refuse are routed to a secondary HTML crawler: transparent UA plus contact, robots.txt respected, gentle, no CAPTCHA/login/403 bypass, facts only. Deferred, but the design keeps the hook.
- The app's datasets are private, and the app stays on HF for now. Migrating to GCP later is easy.
- POC first: draft PRs only until real-world data proves the design. The pooled egress in this environment is labelled as such; the confirmation run happens on Cloud Run default egress.
- Findings for every design (from Design A's real crawl):
  - Resumable Shopify pagination: on a mid-catalog 429, wait Retry-After and continue from that page; limit=250. 11 big stores (Counter Culture, Death Wish, Colectivo, Groundwork, …) failed only because every retry restarted at page 1.
  - Closed or unavailable stores (Shopify's "This store is currently unavailable" page, password-protected stores, a 404 on the whole domain) get a distinct `unavailable` status, not failed or blocked. Example: Big Guns is deactivated. With #63 their last catalog shows for up to 4 weeks, then drops off.
- Raw-layer contract: generate an internal contract for the raw tables (fetches, snapshots, bodies, enrichments, parked, manifests…) from lake.py SCHEMAS, with a test that any raw column change must update it, mirroring the public ODCS contract. This makes the ingestion→dbt boundary explicit for per-connector partitions, the weekly merge and the secondary crawler.
- ELT: ingestion only extracts and loads (raw snapshots, bodies, fetches, manifests). No attribute extraction or classification at crawl time. Move ingest.py's extract.rules()/is_coffee/needs_llm into a separate post-load enrichment step that reads raw; dbt (stg_products) stays the place where rules run. The secondary crawler maps pages/per-product JSON into the same raw record shape (structural parsing only) plus bodies.
- Services: enrichment is its own job after data lands (reads raw from the lake; picks Claude candidates from stg_products logic; submits the batch), separate from ingestion jobs and before the weekly build. Roaster status is derived in the build (dim_coffee/stg_products), not from fetches.coffees. Applied in all three POCs.
- Service architecture principles (owner), for the design doc and all POCs:
  - Clearly separated services, each one job with one responsibility: per-connector ingest (and the secondary crawler), post-load enrichment, the weekly build plus gate, publish/serve.
  - DRY: shared code for the HTTP client, lake I/O, raw schemas, manifests and the adapter interface; no copy-paste per service.
  - Extensible: a new connector is a new adapter plus config, with no changes to the other services.
  - Contracts between services, each versioned and tested:
    1. the raw contract (ingest → lake: snapshots, bodies, fetches, parked);
    2. the manifest/control contract (each job writes a manifest row: job, connector, shard, run, status, counts, egress IP and per-IP refusals, code version);
    3. the enrichment contract (enrichments table);
    4. the public contract (ODCS/ODPS, as today).
  - Control plane: the manifest table is the source of truth for what ran, what landed and what's parked; services read it rather than call each other. Scheduling is Cloud Scheduler. Add an orchestrator (e.g. Prefect) only if dependencies outgrow time-based triggers plus the manifest table.
- Selection rule (owner): the SIMPLEST design that meets the bar wins. The bar is the most data from the roasters at the highest fidelity: the big roasters fetched; each store's coffees and fields matching its best JSON run; every job inside its time limit; the gate passing. Anything more complex is listed as "add when metric X says so".
- Lesson: GitHub-hosted runner IPs drew far more 429s (29% of stores) than other egress (5-8%, pooled). Crawling moves off GitHub runners to Cloud Run (default egress, logged per IP).
- Enrichment submits to Claude only with an explicit flag (e.g. --submit); sims and tests never pass it. (B's sim accidentally sent two real batches from mock data via COFFEEDATA_ANTHROPIC_API_KEY. The owner let them run.)
