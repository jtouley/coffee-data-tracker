# Shopify ingestion: design

Status: POC (draft PR). Nothing here is deployed. The confirmation run on Cloud Run decides whether it ships.

## 1. Decision

**Design B wins: one job per connector, a weekly merge, then enrichment, then the build and gate, then publish.**
Egress is a per-connector setting (Shopify: one dedicated NAT IP; the others: Cloud Run's default egress). Pacing
is per-connector config too: a request budget, a shuffled store order and a crawl window.

The owner's rule is that the simplest design that meets the bar wins. The bar: the big stores complete, every job
inside its time limit, the quality gate passing, and room to extend without overbuilding.

- **C is dropped.** The owner's call: the IP is the lever, not the schedule. C's daily Shopify shards, its retry
  sweep jobs and its extra Scheduler entries cost more and gave no better data (section 2). Its pacing idea stays,
  as per-connector config in B: max requests/s, a shuffled store order, and a crawl window.
- **Between A and B, B wins**, because only B meets the bar on Cloud Run:
  1. **Per-connector egress needs per-connector jobs.** On Cloud Run, egress (default, or Direct VPC through Cloud
     NAT) is set per job. A runs every connector in one job, so Shopify can't get a dedicated IP while the others
     stay on default egress unless A is split into per-connector jobs, which makes it B.
  2. **Big stores complete across jobs.** Parked progress (the pages of a store blocked mid-catalog) has to outlive
     the job that parked it. B already has a persistent raw zone outside HF (GCS on Cloud Run), and parked progress
     lives there. A keeps it in the lake's local staging, which is lost between Cloud Run jobs unless A adds the same
     zone.
  3. **Contracts and the control plane.** B has the manifest/control contract the owner asked for: `job_manifests`
     records the job, connector, status, counts, egress IPs, per-IP refusals and code version. Its three internal
     contracts are versioned and tested. A has neither.
  4. **The real data doesn't separate them** (section 2): A had 97.4% [96.2, 98.2] ok and B had
     96.3% [94.9, 97.2]. The intervals overlap, and A's run had two advantages that come from conditions and shared
     code, not from its design.
- **What B costs over A:** 4 Cloud Run job definitions instead of 3, more Scheduler entries (section 7), and a merge
  step. Anything more is listed under "add when metric X says so" (section 12).

## 2. Comparison

### Real crawls: all 1,070 ingestable Shopify roasters (1,058 hosts), 2026-10-31, this environment's pooled egress

| | A (run 2, final) | B | C (7 shards + sweep) |
|---|---|---|---|
| When (UTC) | 18:26–19:01 | 19:21–20:05 | shards 20:25–23:25, sweep from 23:25 (see note) |
| Lake history (last known product counts) | **yes**: seeded with A's run 1 | none | none |
| Shopify rate | 3 req/s, 32 workers | 3 req/s, 32 workers | 1.5 req/s per shard |
| Code | #62 + v1 + v2 (18:06) + A's burst fix | #62 + v1 + v2, frozen before ELT | #62 + v2 (18:24), old burst rule |
| Wall | 34.8 min (job 27.1 + crawl-retry 7.7) | 44.1 min (job 26.0 + crawl-retry 18.1) | 7 × 11.5–13.0 min (86.5 min of jobs over 3 h), plus the sweep |
| Requests sent | 3,005 | 4,101 | 3,673 (shards) + sweep |
| 429 / 430 / 503 | 670 / 0 / – | 816 / 0 / 17 | 504 / 0 / 2 (shards) + sweep |
| First catalog request answered 429 | 205 of 1,044 = 19.6% [17.3, 22.2] | 230 of 1,044 = 22.0% [19.6, 24.6] | shards 0–4: 146 of 753 = 19.4%; shards 5–6: 14 of 312 = 4.5% |
| **ok** | **1,042 = 97.4% [96.2, 98.2]** | **1,030 = 96.3% [94.9, 97.2]** | **1,027 = 96.0% [94.6, 97.0]** before the sweep ends |
| Refused (page 1 never served) | 14 | 20 | 26 |
| Parked mid-catalog (pages kept) | 12 | 18 | 14 |
| Other failures | 2 (javamasters 404, biggunscoffee robots) | 2 | 3 |
| Platform-wide pauses | 0 | 0 in the job, 7 in crawl-retry | 1 per shard in 0–2 and 4, several in the sweep |
| Every job inside 240 min | yes | yes | yes |
| A's 11 page-2 failures from run 1, now ok | 7 | 4 (3 more expected with history) | 6 |

All three runs were per-store in pattern: every Retry-After was 60, robots.txt never got a 429, and there were no
430s. The pooled egress IP wasn't logged in any run, which is why this PR adds IP logging.

The C sweep is still running. Its final numbers are added in section 2.1 when it ends.

**Where the differences come from (not the design):**
- **History.** v2's short-page stop ends a catalog at a short page only once the store's last known product count is
  reached. A's run 2 had that history; B and C didn't, so every store also asked for its empty last page. That
  means more requests and more 429s, and some stores parked on the empty page (B: Counter Culture, Colectivo and
  Coffee AM were served page 1 and parked on page 2). This favours A, and is shared code.
- **Burst rule.** A's run carried a fix that B and C don't have: only a store's first block in a run counts towards
  platform-wide detection. C's logs show the old rule tripping a 60 s pause of all Shopify and a cut to 0.38–0.75
  req/s in most shards and in the sweep. B tripped 7 pauses in crawl-retry. This is shared code (ratelimit.py) and
  can go to any design (follow-up 3).
- **Time of day and earlier traffic.** The runs were 1–8 h apart, against the same stores from the same egress, so
  per-store windows from the earlier runs may still have been open. C's shards 5–6 (22:29 and 23:13) saw 4.5%
  first-contact 429s against 19.4% in shards 0–4. Shards 0–4 ran at half A's and B's rate and still matched their
  ~20%. The drop in 5–6 came with a later hour and with 32-minute gaps instead of 2–16, so the cause can't be
  pinned down from one night.
- **Egress IP.** It was the same pooled egress, but the IP(s) weren't recorded, so equal IPs can't be shown.

### Simulation (localhost mocks, the same scenario for all, 2,545 stores, clock ×10)

| | A | B | C |
|---|---|---|---|
| End-of-week ok, S1 (Shopify) | 96.4% (93.3%) | 96.4% (93.2%) | 96.5% (93.4%) |
| End-of-week ok, S2: an IP-wide limit of 2 req/s | 96.4% with history; **86.4% on a first run (gate fails)** | 93.7% (Shopify 86.7%: v2's backed-off stores never trip the platform pause, and 433 were parked) | 96.5% (1.5 req/s stays under the limit) |
| Longest job | 48.5 min | 24 min (S1), 44 min (S2) | 12.9 min |
| Inside 240 min | yes | yes | yes |
| Refused | 65 | 66 | 65 |
| Woo outage / kill at 50% | recovered by Tuesday's resume; the rerun redid ~400 finished roasters | 0% in the outage slot, 97.4% in its resume slot; the kill kept 200 of 349 | the sweep recovered 227 of 233; the re-run skipped 48 checkpointed |
| dbt marts unchanged under ELT | 34 models row for row | 17 tables identical | 26 tables row for row |
| Scheduler entries / jobs | ~4 / 3 | 13 / 4 definitions | 11 / 7 |
| New connector (bigcartel) | ~6 files, limiter wired into ingest.run | 4 files: adapter + 2-line registration + one `Connector(...)` line | adapter + TOML block + 4 lines |
| Manifest/control contract | no | yes (`job_manifests`, versioned) | yes (`control/v1`) |

In S2, B's result and A's first-run failure are both the shared v2 pagination code under an IP-wide limit: stores in
page backoff stay quiet. C avoids it only by its lower rate. On a dedicated IP, an IP-wide limit is driven by our
own rate, so it is set by B's per-connector `rps` (section 8).

### 2.1 C sweep: final numbers

_Pending: the sweep ends at about 03:25 UTC; this section is updated then._

## 3. Services (one job, one responsibility)

| Service | Command | Reads | Writes |
|---|---|---|---|
| Connector ingest | `coffeedata crawl --connector X` | registry, lake history, its partition | `raw/connector=X/run_date=W/` (snapshots, bodies, fetches, run_manifests), parked progress, `job_manifests` |
| Parked-store retry | `coffeedata crawl-retry --connector X` | its parked progress | the same partition |
| Weekly merge | `coffeedata merge` | `job_manifests` (who landed), the partitions | the lake's `run_manifests/ fetches/ snapshots/ bodies/run_date=W/`: today's raw-run shape |
| Post-load enrichment | `coffeedata enrich-week [--submit]` | `job_manifests` (merge done), raw rows | `enrichments`, `llm_batches` |
| Build + gate, publish | `coffeedata build`, `coffeedata publish` | the lake | marts, release, `builds` |
| Egress check | `coffeedata egress-check` | `job_manifests` | exit code, then an email to the owner |
| Secondary crawler (deferred) | `coffeedata pages` (#67) | refused stores | the same raw shape |

The shared code is the HTTP client (`http.py`), lake I/O and `SCHEMAS` (`lake.py`), the adapter interface,
`ingest.run`, the control plane (`control.py`) and the coffee decision (`products.py`, used by dbt, enrichment and
the registry). There is no per-service copy.

**New connector** = `adapters/<name>.py` + an `ADAPTERS` entry + the platform mapping in `models.py` + one
`Connector(...)` line (its schedule, rps, egress, crawl window). Merge, enrichment and build don't change.

## 4. ELT

- **Ingest only extracts and loads**: snapshots, bodies, fetches and manifests. It extracts no attributes and
  classifies nothing at crawl time. `fetches.coffees` and `snapshots.is_coffee` stay null in new rows; the columns
  remain for the raw shape.
- **Enrichment is a separate job after the load.** It reads raw from the lake and picks Claude's candidates with the
  build's own rules (`products.py`, shared with `stg_products`). It runs after the merge and before the build.
- **Enrichment submits to Claude only with an explicit `--submit`**, and only with credentials. Without it, it
  counts candidates and sends nothing. Sims and tests never pass it. (One sim sent two real batches before this guard
  existed; the owner let them run.)
- **dbt stays the place where rules run.** Roaster status is derived in the build (`dim_roaster.sql`,
  `registry.STATE_SQL`, both via `products.py`), not from `fetches.coffees`. That is the only dbt change, and it
  is owner-approved; app/ and contracts/ are unchanged.
- **Checked on a local copy of the lake** (runs 10-26 to 10-30), with `coffeedata build` on `main` and on this
  branch:
  - 34 models and 76 tests each, with the same gate outcome for every run date: 10-26 passes, and 10-27, 10-28 and
    10-30 fail the same completeness and ok-rate checks on both, because those runs are partial in the lake;
  - 32 of the 34 built tables are identical;
  - the other two, `dim_roaster` and `public_roaster`, differ only in the status of 4 roasters. Each new status now
    agrees with the roaster's listed coffees. steamshovelcoffee.com (1 coffee listed) and sumerce.coffee (4) go
    from `no_coffee` to `active`. kaldi.com and hiker.coffee (0 listed on `main` too) go from `active` to
    `no_coffee`.

## 5. Contracts (versioned and tested)

`src/coffeedata/raw_contracts.py` generates the three internal contracts from `lake.SCHEMAS`. Each committed JSON
file carries a version and a fingerprint. `tests/test_raw_contracts.py` fails on any column change until the file is
regenerated with a bumped version (`python -m coffeedata.raw_contracts --write <name> <version>`). It also checks
that each producer writes exactly its contract's columns.

| Contract | Version | Tables / files | Producer → consumers |
|---|---|---|---|
| **raw** | **1.1.0** (this PR adds `fetches.blocks`) | `snapshots` (run_date, batch_id, fetched_at, roaster_id, product_id, handle, url, title, platform, vendor, product_type, tags, image, body_hash, variants, is_coffee, published_at, payload_hash); `bodies` (body_hash, body_text, first_seen_run); `fetches` (run_date, batch_id, fetched_at, roaster_id, adapter, status, error, products, coffees, pages, requests, http_statuses, retries, rate_limited, duration_s, code_version, **blocks**); `parked/run_date=<d>/<roaster_id>.json` (page, items) | connector jobs via the merge → dbt (stg_snapshots, stg_product_inputs, stg_fetches), enrichment, registry |
| **manifest / control** | 1.0.0 | `run_manifests` (run_date, manifest_id, started_at, roaster_id, status, platform, adapter, mode, code_version); `job_manifests` (run_date, job, connector, shard, attempt_id, started_at, ended_at, status, todo, ok, errors, parked, skipped, requests, http_429, http_430, egress_ips, refusals[ip, stores, refused], code_version, detail) | every job → dbt run_completeness, merge, enrichment, egress-check |
| **enrichment** | 1.0.0 | `enrichments` (extracted_at, roaster_id, product_id, body_hash, method, needs_llm, origin_country, origin_region, producer, process, varietals, elevation_masl_min/max, roast_level, tasting_notes, is_blend, is_decaf) | enrich-week → dbt stg_enrichments, dim_coffee |
| **public** | as today | `contracts/public-catalog.odcs.yaml` (ODCS/ODPS) | build → the dataset; `contracts/test_contracts.py`, unchanged |

`fetches.blocks` is new and nullable, so older files read back as null. `stg_fetches` selects named columns, so dbt
is unaffected.

**Control plane.** `job_manifests` is the source of truth for what ran, what landed and what's parked. Each job writes
a row when it starts (`running`) and one when it ends (`done`, `deadline`, `failed`). An attempt with only the first
row died. Services read it instead of calling each other: the merge sees which connectors landed, enrichment sees
that the merge ran, and egress-check sees per-IP refusals.

## 6. Shopify crawling (the shared crawler fixes, PAGINATION v2)

Every design got the same fixes, and they are in this PR:
- `limit=250`, `MAX_PAGES` 60 (kaldi.com needs more than 20 pages);
- **resumable pagination**: a 429 mid-catalog waits Retry-After and continues from that page. The page backoffs are
  60/120/240 s and hold no worker. After 3 backoffs the store is parked with its pages kept, including their
  descriptions, and `crawl-retry` resumes it from the blocked page. The 11 big stores (Counter Culture, Death Wish,
  Colectivo, Groundwork, …) failed only because every retry restarted at page 1;
- **the conditional short-page stop**: a short page ends the catalog only once the store's last known product count
  is reached (onyxcoffeelab.com: all 991 products);
- per-store backoff and deferred rounds (#62). A store refused on every try is labelled `refused`.

## 7. Cloud Run Jobs + Cloud Scheduler (no Prefect)

One image. Four job definitions; the task timeout is 240 min (the strict limit used in every sim) and maxRetries is 1.

| Job | Args | Network | Scheduler (UTC) |
|---|---|---|---|
| `crawl-shopify` | `crawl --connector shopify`, then `crawl-retry --connector shopify` 6 h later | **Direct VPC egress → Cloud NAT, one reserved IP** (`egress="dedicated"`) | Mon 02:00, Mon 08:00 |
| `crawl` (other connectors) | `crawl --connector X` (args overridden per execution), resume 6 h later | default egress | B's tested slots: Tue–Fri 02:00 + 08:00 |
| `merge` | `merge` | default | Sun 06:00 |
| `enrich` | `enrich-week --submit` | default | Sun 07:00 |
| `build` | `build` then `publish`, then `egress-check` | default | Sun 09:00 |

That is 13 Scheduler entries, as B was simulated. Every connector has its own job, so the other connectors could
run on Monday alongside Shopify. That would put a week's fetches closer together under one run_date. It's a
schedule change with no code change; make it if the confirmation run shows the jobs don't interfere.

raw/ and parked/ live in GCS (mounted or `COFFEEDATA_RAW`), so parked progress survives between jobs. The merge
publishes one file per raw table to HF in one commit. Prefect is out for now: the jobs are containers, so adding it
later is a wrapper. Add it only if the dependencies outgrow time-based triggers plus the manifest table.

## 8. Pacing (per-connector config, from C)

`connectors.Connector` holds each connector's pacing:
- `rps`: the platform-wide request budget (Shopify: `SHOPIFY_RPS`);
- `shuffle`: stores in a shuffled order, seeded by connector and week, so the same stores aren't always the last
  ones asked (new in this PR);
- `deadline_min`: the crawl window, i.e. how long one job may start new stores, up to its time limit (225 of 240 min).

"Spread the crawl over hours" is **anecdotal forum advice, not Shopify's** (research §3: Shopify publishes no
numbers). On shared egress, C's spread-out crawl at half the rate showed no drop in 429s: shards 0–4 had 19.4%
first-contact 429s, against 19.6% for A and 22.0% for B. Shards 5–6 dropped to 4.5%, but with a later hour and
longer gaps (section 2), so that is unexplained, not evidence for spreading. On a **dedicated IP**, our own rate
decides that IP's reputation. So the confirmation run tests full speed against a low, spread-out rate on the
dedicated IP (section 10). If the low rate wins, it's a config change: `rps` and `deadline_min`.

## 9. Egress

- **Per connector, in config**: `Connector.egress` is `"dedicated"` for Shopify and `"default"` for the rest. The
  deploy sets the job's network from it, and the job records it in `job_manifests.detail.egress`. It's one design
  with a setting, not a different design per connector. The other connectors stay on default egress unless their
  data shows otherwise.
- **Every run logs each egress IP and its refusals** in `job_manifests`: `egress_ips` lists every IP seen (at start,
  at end, and on blocks), and `refusals[ip, stores, refused]` counts first-contact refusals (429/430 before any page)
  per store, attributed to the IP its first block left from. The IP comes from `COFFEEDATA_EGRESS_IP` (a dedicated NAT
  IP is known in advance) or from `COFFEEDATA_EGRESS_ECHO_URL`, a plain "what is my IP" endpoint, never a store. That
  lookup is cached 60 s, since default egress can change IP between connections. With neither set, the IP is
  recorded as unknown.
- **The 8% alert**: `coffeedata egress-check` flags an IP with more than 8% first-contact refusals on two runs in a
  row. The owner is emailed (the job exits non-zero, and a Cloud Monitoring alert on the job's failure sends the
  mail). There is no automatic action.
- **Escalation, only if the data says so**: Cloud NAT with 1 IP, then N IPs, then a separate egress-swap job. Never
  rotate IPs to dodge limits.

### Block-header logging (this PR)

Every 403, 429, 430 or 503 response, robots.txt and redirects included, adds a record to its fetch row,
`fetches.blocks` (at most 20 per fetch). Each record holds: `at`, `status`, `host`, `path`, `server`,
`cf_mitigated`, `content_type`, `retry_after`, `cf_ray`, `x_request_id` and `egress_ip`. That answers the research's
open questions from our own logs:
- is a block a Cloudflare challenge (`cf-mitigated: challenge`, an HTML body, often no Retry-After) or a rate
  limit (JSON, `retry-after: 60`)?
- does it cluster by IP or by hour?

It also gives Shopify Support what it asks for: the UTC time, `x-request-id`, `cf-ray` and the source IP. Tested in
`tests/test_block_headers.py`.

## 10. Confirmation run (Cloud Run, before anything ships)

The question: does one dedicated NAT IP beat default egress for Shopify, and at which rate?

| Arm | Egress | Rate |
|---|---|---|
| 1 | Cloud Run default | full (`SHOPIFY_RPS`, as today) |
| 2 | one dedicated NAT IP | full |
| 3 | one dedicated NAT IP | low and spread out (e.g. 0.5 req/s over a longer `deadline_min`) |

- **The same stores in every arm.** Split all Shopify roasters deterministically: a salted hash of `roaster_id`
  into thirds, one per arm. Record the salt. Rotate the arms across runs so each third sees each arm.
- **The same conditions.** Lake history present in every arm. Each arm a day apart, at the same hour, so per-store
  windows have reset.
- **Metrics per arm**, from `job_manifests` and `fetches.blocks`:
  - first-contact refusal share per IP, with Wilson 95% intervals;
  - ok share, refused, parked and wall time;
  - the share of blocks that are challenges (`cf_mitigated`) and that carry Retry-After.
- **Decision.** Dedicated egress stays if arm 2's refusal share is lower than arm 1's with intervals that don't
  overlap, or if it is equal while arm 1 shows challenges. Arm 3 becomes the config if it beats arm 2 inside the
  time limit.

## 11. Closed and unavailable stores

A closed store is a distinct status, `unavailable`, not `failed` or `blocked`. Signals:
- Shopify's "This store is currently unavailable" page;
- a password-protected storefront (a redirect to `/password`);
- a 404 for the whole domain, or a DNS failure for the host.

The status is set only if the signal repeats on two runs at least a day apart. Example: Big Guns is deactivated, and
its robots.txt now disallows the feed. With #63, a store's last catalog shows for up to 4 weeks, then drops off.
`unavailable` stores are excluded from the egress refusal rate and from the gate's ok-rate denominator. The
detection is a follow-up (follow-up 2); this PR doesn't change status labels.

## 12. Next steps from the research, and the secondary crawler

- **Web Bot Auth, then Shopify's higher-limits form** (Shopify changelog 2026-05-07: unsigned bots get the strictest
  limits). Ed25519 key, a key directory on our domain, `Signature-Agent` as a quoted string, then the form. Forum
  reports say registration takes days to weeks, and that a signature alone isn't recognized until the bot is
  registered. **Operator details are the owner's to supply**: the domain for the key directory, the contact address,
  and the bot page.
- **Honest UA**: keep `coffee-data/0.1 (+URL)`, and add a contact and a bot page (purpose, rate, opt-out).
- **The secondary crawler is a separate component**: draft PR #67 (`coffeedata pages`), for stores whose JSON feed
  refuses every request. Its rules: a transparent UA with contact, robots.txt respected, 3 s between requests, the
  first 401/403/429/430/503 or challenge stops the store, no CAPTCHA/login/403 bypass, facts only. It maps pages and
  per-product `.js` into the same raw shape, plus bodies. Its first pass found 255 of 926 best-run coffees (28%)
  across 14 stores. 4 stores passed with every field, and 503s stopped 6 of them. Those 14 stores were development
  data, so it needs a held-out, roaster-grouped score before it ships.
  - **The dbt `partial` gap**: a fetch is either complete or an error. A secondary crawl cut off by a 503 (Coffee
    King 60 of 74, Artizan 61 of 160) can only be dropped, since landing it as ok would make the missing products
    look delisted. dbt needs a `partial` fetch status, meaning these products were seen and absence means nothing,
    before partial catalogs can land.
  - The hook in B: refused stores are labelled in `fetches` and visible in `job_manifests`. The secondary crawler is
    one more connector job that reads them.
- **The Shopify API terms need a legal read.** The Shopify API Terms (updated 2026-10-07) bar using the API "to build
  any commerce or product index" and systematic data collection without the merchant's grant. That rules out
  tokenless Storefront API and Catalog MCP as primary sources on a plain reading, and the Ajax `.js` API is
  documented for themes. #67 uses `.js` per product. Get a legal read before relying on any of them, including
  #67's `.js` path.

## 13. Follow-ups

1. **Confirmation run** (section 10) on Cloud Run: default egress against one dedicated NAT IP, and full speed
   against a low rate. Deploy the 4 jobs and the Scheduler entries.
2. **`unavailable` status** (section 11), with tests.
3. **Burst rule from A**: only a store's first block counts towards platform-wide detection, and a 430 still pauses
   the platform. Also, distinct stores blocking together should count even while in page backoff (B's S2 weakness).
   Shared ratelimit.py, measured in the S2 sim before and after.
4. **Refused stores skip to crawl-retry** once refused this week, instead of 3 backoffs per job (7 min each).
5. **Web Bot Auth + the higher-limits form**, once the owner supplies the operator details.
6. **Secondary crawler** (#67) as its own connector job, after the dbt `partial` status and a held-out score.
7. **The API terms legal read**, before any `.js`, Storefront API or Catalog MCP use.
8. **Conditional requests**: send `If-None-Match` and `If-Modified-Since` where an ETag is known, and log the share
   of 304s.
9. **Lock the control writes** with GCS generation preconditions, so two attempts can't race on `job_manifests`.
10. **Same-day connector schedule** (section 7), if the confirmation run shows the jobs don't interfere.
