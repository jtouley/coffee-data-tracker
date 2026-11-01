# Ingestion: design (Shopify first)

Status: POC (draft PR). The Cloud Run jobs, the NAT and its egress IP (34.172.156.113) are applied in production
with the schedule off (RUNBOOK.md §2); nothing crawls on a schedule from Cloud Run yet. The confirmation run on Cloud
Run decides whether the cutover ships.

**Terms used below.**
- **A, B, C**: the three designs compared. **A** = one omnibus job for every connector. **B** = one job per connector,
  then a weekly merge. **C** = Shopify split into 7 daily shards, plus retry sweeps.
- **S1, S2**: the two simulation scenarios. S1 = per-store limits only. S2 = S1 plus an IP-wide limit of 2 req/s.
- **v1, v2**: the Shopify pagination fixes. v1 = resumable pagination (a 429 mid-catalog continues from that page);
  v2 = v1 plus the conditional short-page stop (section 6).
- **Pooled egress**: an outbound IP shared with other tenants (this research environment's egress, Cloud Run's
  default egress, GitHub-hosted runners), as opposed to one dedicated NAT IP only we use.
- **The owner**: the repository's maintainer, who runs the runbook. **#62, #67**: earlier and companion PRs
  (#62 per-store backoff and deferred rounds; #67 the secondary crawler). "Research §n" is a section of
  [`evidence/research/shopify-503.md`](https://github.com/jtouley/coffee-data-tracker/blob/archive/shopify-arch-evidence/evidence/research/shopify-503.md)
  on the evidence branch.

## 1. Decision

**Design B wins: one job per connector, a weekly merge, then enrichment, then the build and gate, then publish.**
Egress is a per-connector setting (Shopify: one dedicated NAT IP; the others: Cloud Run's default egress). Pacing
is per-connector config too: a request budget, a shuffled store order and a crawl window.

The owner's rule is that the simplest design that meets the bar wins. The bar: the big stores complete, every job
inside its time limit, the quality gate passing, and room to extend without overbuilding.

- **C is deferred, not rejected** ("Deferred: daily shards (C)" below). On shared egress the IP is the lever, not
  the schedule. Its pacing idea stays, as per-connector config in B: max requests/s, a shuffled store order and a
  crawl window. C becomes a candidate again once Shopify is on a dedicated IP.
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
- **What B costs over A:** one Cloud Run job per connector instead of one omnibus job, more Scheduler entries
  (section 7), and a merge step. Anything more waits for a metric that calls for it (the "Next" list, section 13).

## 2. Comparison

The evidence is archived on pushed branches, not copied into this repo's docs:
- [`archive/shopify-arch-evidence`](https://github.com/jtouley/coffee-data-tracker/tree/archive/shopify-arch-evidence/evidence)
  holds each design's notes, sim and real-crawl results, the response logs (no page bodies), the analysis scripts,
  and the research. Start at its `evidence/README.md`.
- [`archive/shopify-arch-a`](https://github.com/jtouley/coffee-data-tracker/tree/archive/shopify-arch-a),
  [`-b`](https://github.com/jtouley/coffee-data-tracker/tree/archive/shopify-arch-b) and
  [`-c`](https://github.com/jtouley/coffee-data-tracker/tree/archive/shopify-arch-c) hold each design's code as it
  ran.

### Real crawls: all 1,070 ingestable Shopify roasters (1,045 hosts), 2026-10-31, this environment's pooled egress

| | A (run 2, final) | B | C (7 shards + sweep) |
|---|---|---|---|
| When (UTC) | 18:26–19:01 | 19:21–20:05 | shards 20:25–23:25, sweep 23:25–01:34 |
| Lake history (last known product counts) | **yes**: seeded with A's run 1 | none | none |
| Shopify rate | 3 req/s, 32 workers | 3 req/s, 32 workers | 1.5 req/s per shard |
| Code | #62 + v1 + v2 (18:06) + A's burst fix | #62 + v1 + v2, frozen before ELT | #62 + v2 (18:24), old burst rule |
| Wall | 34.8 min (job 27.1 + crawl-retry 7.7) | 44.1 min (job 26.0 + crawl-retry 18.1) | 7 × 11.5–13.0 min (86.5 min of jobs over 3 h) + sweep 128.3 min |
| Requests sent | 3,005 | 4,101 | 4,135 (shards 3,673 + sweep 462) |
| 429 / 430 / 503 | 670 / 0 / – | 816 / 0 / 17 | 779 (shards 504 + sweep 275) / 0 / 6 |
| First catalog request answered 429 | 205 of 1,044 = 19.6% [17.3, 22.2] | 230 of 1,044 = 22.0% [19.6, 24.6] | shards 0–4: 146 of 753 = 19.4%; shards 5–6: 14 of 312 = 4.5% |
| **ok** | **1,042 = 97.4% [96.2, 98.2]** | **1,030 = 96.3% [94.9, 97.2]** | **1,027 = 96.0% [94.6, 97.0]** |
| Refused (page 1 never served) | 14 | 20 | 27 (routed to the HTML stub) |
| Parked mid-catalog (pages kept) | 12 | 18 | 13 |
| Other failures | 2 (javamasters 404, biggunscoffee robots) | 2 | 3 |
| Platform-wide pauses | 0 | 0 in the job, 7 in crawl-retry | 1 per shard in 0–2 and 4, several in the sweep |
| Every job inside 240 min | yes | yes | yes |
| A's 11 page-2 failures from run 1, now ok | 7 | 4 (3 more expected with history) | 6 |

All three runs were per-store in pattern: every Retry-After was 60, robots.txt never got a 429, and there were no
430s. The pooled egress IP wasn't logged in any run, which is why this PR adds IP logging.

C's final numbers include its retry sweep (section 2.1).

**Where the differences come from (not the design):**
- **History.** v2's short-page stop ends a catalog at a short page only once the store's last known product count is
  reached. A's run 2 had that history; B and C didn't, so every store also asked for its empty last page. That
  means more requests and more 429s, and some stores parked on the empty page (B: Counter Culture, Colectivo and
  Coffee AM were served page 1 and parked on page 2). This favours A, and is shared code.
- **Burst rule.** A's run carried a fix that B and C don't have: only a store's first block in a run counts towards
  platform-wide detection. C's logs show the old rule tripping a 60 s pause of all Shopify and a cut to 0.38–0.75
  req/s in most shards and in the sweep. B tripped 7 pauses in crawl-retry. This is shared code (ratelimit.py), and
  this PR now carries A's fix (section 6).
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

The sweep ran 23:25–01:34 UTC (128.3 min, inside its limit) over the 57 stores the shards had parked or refused.
It ran in 4 waves (+0, 15, 60 and 120 min, each after the stores' Retry-After):

| wave | due | ok | routed as refused | requests | rate-limited |
|---|---|---|---|---|---|
| 0 | 49 | 14 | 16 | 283 | 148 |
| 1 | 8 | 0 | 0 | 45 | 32 |
| 2 | 18 | 2 | 5 | 90 | 67 |
| 3 | 8 | 0 | 6 | 44 | 28 |

- **Recovered:** 16 of 57 (28%). 13 are still parked, 27 were routed to the HTML stub as refused (2 jobs ≥ 1 h
  apart), and 1 failed with another error: 16 + 13 + 27 + 1 = 57.
- **The rate didn't help.** The sweep sent 462 requests and got 275 429s (60%), 0 430s and 6 503s. Its first
  contacts were refused on 45 of 57 stores (79%), at 0.39–0.75 req/s after its platform-wide pauses.
- **C's final result for all 1,070 stores: 1,027 ok = 96.0% [94.6, 97.0]**, with 27 refused, 13 parked and 3 other
  errors. Of A's 11 page-2 stores (from A's run 1), 6 are ok. Of the 22 stores A's run 1 refused (the table shows A's
  run 2, with 14), 3 are ok, 6 parked and 13 refused. C's 27 refused overlap 13 of those 22 and 9 of B's 20.

## Deferred: daily shards (C)

Design C splits Shopify into 7 daily shards (sha256(id) mod 7, one per weekday), each at 1.5 req/s, with retry-sweep
jobs and an HTML hook: 11 Scheduler entries. **It is deferred, not rejected: it's premature.** On shared egress, the
evidence says the IP is the lever now, not the schedule. One signal says pacing might still matter.
- **First-contact 429s ran at about 20% in all three designs on shared egress**: A 19.6%, B 22.0%, and C's shards
  0–4 19.4%, even at C's half rate. C's shards 5–6 dropped to 4.5%, but they ran once, later, and after longer gaps,
  so they don't show that spreading works (section 2).
- **Retrying on shared egress yields little.** C's sweep covered 57 candidates in 4 waves over 128 min:
  - 16 recovered (ok), 27 were routed as refused, and 13 are still parked;
  - 275 of its 462 requests (60%) were rate-limited;
  - its first contacts were refused on 45 of 57 stores (79%) at 0.39–0.75 req/s (section 2.1).

  Those stores had already refused, so the 79% isn't comparable to the population's 20%. It shows that a low rate
  didn't clear their refusals.
- **Shopify staff tie challenges to shared-IP reputation, not to request rate** (research §1, thread 39056: "the
  bot's request rate did not cause the challenge"; dedicated egress is "the safest workaround").

**Why it is deferred, not rejected.** The first-contact 429 rate fell from 16–27% on shards 0–4 to ~4.5% on shards
5–6 (7 of 159 and 7 of 153), after 32-minute gaps. It is confounded with the time since A's and B's crawls ended
(shards 5–6 started 2.5–3 h after B), so it could be per-store windows closing rather than C's spacing. But it's
the one signal that pacing might matter. The confirmation run's arm 3 tests only part of it: a **low rate** on the
dedicated IP, not stores spread over hours (section 10).

Once Shopify is on a dedicated IP, our own rate decides that IP's reputation, and C
becomes a candidate again. Until then its pacing lives as per-connector config in B (rps, a shuffled order, a
crawl window, section 8).

**The trigger to revisit C:** in the confirmation run (section 10), the low rate on the dedicated IP gets a higher ok
share than full rate, with Wilson intervals that don't overlap, **and** B can't fit that pacing for a whole week's
Shopify crawl inside one job's 240-min limit. If the low rate fits in one job, it stays a config change in B. Testing
C's spacing itself (stores spread over hours) needs a main-pass spread in B first (Next).

C's code and results are on the archive branches: its code on
[`archive/shopify-arch-c`](https://github.com/jtouley/coffee-data-tracker/tree/archive/shopify-arch-c), and its real
crawl (logs, jobs, control manifests) and sim under `evidence/arch-compare/C` on
[`archive/shopify-arch-evidence`](https://github.com/jtouley/coffee-data-tracker/tree/archive/shopify-arch-evidence/evidence).
C never had a PR.

## 3. Services (one job, one responsibility)

| Service | Command | Reads | Writes |
|---|---|---|---|
| Discovery, probing | `coffeedata discover overture\|osm`, `coffeedata probe` | open datasets, stale roasters' sites | `registry`, `probes` |
| Connector ingest | `coffeedata crawl --connector X` | registry, lake history, its partition | `raw/connector=X/run_date=W/` (snapshots, bodies, fetches, run_manifests), parked progress, `job_manifests` |
| Parked-store retry | `coffeedata crawl-retry --connector X` | its parked progress | the same partition |
| Weekly merge | `coffeedata merge` | `job_manifests` (who landed), the partitions | the lake's `run_manifests/ fetches/ snapshots/ bodies/run_date=W/`: today's raw-run shape |
| Post-load enrichment | `coffeedata enrich-week [--submit]` | `job_manifests` (merge done), raw rows | `enrichments`, `llm_batches` |
| Build + gate, publish | `coffeedata build`, `coffeedata publish` | the lake | marts, release, `builds` |
| Egress check | `coffeedata egress-check` | `job_manifests` (production crawls; arms are left out) | exit code (no alert policy yet: Next) |
| Secondary crawler (deferred) | `coffeedata pages` (#67, not in this branch) | refused stores | the same raw shape |

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
- **Roaster status is unchanged from `main`.** `main` reads the coffee count each crawl recorded
  (`fetches.coffees`), which an ELT crawl no longer fills. dbt now recreates that count for such a fetch in
  intermediate models, with the crawl's own rule (`classify.is_coffee`, the rules alone: no verdicts, not the
  listed count, no suspect-run filter): `int_fetch_product_inputs` → `int_fetch_coffees` → `int_fetches`
  (`coalesce(recorded, counted)`). `dim_roaster.sql` is `main`'s, except that it reads `int_fetches` instead of
  `stg_fetches`; `registry.STATE_SQL` counts the same way. Moving status out of the dim is a follow-up (section 13).
- **Checked on a local, read-only copy of the lake** (runs 10-26 to 10-30), with `coffeedata build` on `main` and on
  this branch, for run dates 10-26 and 10-30:
  - every table both builds share is identical row for row, on every column they share (build ids aside);
  - the branch only adds columns (`stg_fetches.partial`, `stg_runs.partial`/`is_current`/`prev_complete_run`/
    `next_complete_run`, `stg_run_walk.partial`,
    `stg_run_quality.partial`, `int_roaster_runs.unavailable`, `run_completeness.unavailable`) and the three
    intermediate models above, which are empty on this lake (every fetch in it recorded its count);
  - the gate outcome is the same on both: 10-26 passes; 10-30 fails the same completeness and ok-rate checks,
    because that run is partial in the lake.
  - Roaster status is `main`'s, including kaldi.com and hikersbrewcoffee.com (hiker.coffee): both `active`.
    kaldi.com's 10-26 fetch hit the old 20-page cap (5,000 products, all equipment parts such as BUNN kits; its
    coffees come later in the catalog). The crawl's rules then counted 3,459 of them as coffee; today's rules count 0,
    so it lists 0 coffees while its status, from the recorded count, is `active`. The live store sells coffee
    (24 products in /collections/latin-american-gourmet-coffee). steamshovelcoffee.com and sumerce.coffee stay
    `no_coffee` as on `main`, though their live stores sell coffee and the build lists 1 and 4: the status
    follow-up fixes both kinds of mismatch.

## 5. Contracts (versioned and tested)

`src/coffeedata/raw_contracts.py` generates the three internal contracts from `lake.SCHEMAS`. Each committed JSON
file carries a version and a fingerprint. `tests/test_raw_contracts.py` fails on any column change until the file is
regenerated with a bumped version (`python -m coffeedata.raw_contracts --write <name> <version>`). It also checks
that each producer writes exactly its contract's columns.

| Contract | Version | Tables / files | Producer → consumers |
|---|---|---|---|
| **raw** | **1.2.0** (this PR adds `fetches.blocks` in 1.1.0, then `fetches.partial` and the `unavailable` status in 1.2.0) | `snapshots` (run_date, batch_id, fetched_at, roaster_id, product_id, handle, url, title, platform, vendor, product_type, tags, image, body_hash, variants, is_coffee, published_at, payload_hash); `bodies` (body_hash, body_text, first_seen_run); `fetches` (run_date, batch_id, fetched_at, roaster_id, adapter, status, error, products, coffees, pages, requests, http_statuses, retries, rate_limited, duration_s, code_version, **blocks**, **partial**); `fetches.status` ∈ ok, error, unavailable; `parked/run_date=<d>/<roaster_id>.json` (page, items) | connector jobs via the merge → dbt (stg_snapshots, stg_product_inputs, stg_fetches), enrichment, registry |
| **manifest / control** | 1.1.0 (descriptions and quality rules) | `run_manifests` (run_date, manifest_id, started_at, roaster_id, status, platform, adapter, mode, code_version); `job_manifests` (run_date, job, connector, shard, attempt_id, started_at, ended_at, status, todo, ok, errors, parked, skipped, requests, http_429, http_430, egress_ips, refusals[ip, stores, refused], code_version, detail) | every job → dbt run_completeness, merge, enrichment, egress-check |
| **enrichment** | 1.1.0 (descriptions and quality rules) | `enrichments` (extracted_at, roaster_id, product_id, body_hash, method, needs_llm, origin_country, origin_region, producer, process, varietals, elevation_masl_min/max, roast_level, tasting_notes, is_blend, is_decaf) | enrich-week → dbt stg_enrichments, dim_coffee |
| **public** | 2.2.0 (`is_listed` keeps a partial catalog's unread coffees listed; no column or enum change) | `contracts/public-catalog.odcs.yaml` (ODCS/ODPS) | build → the dataset; `contracts/test_contracts.py` |

`fetches.blocks` and `fetches.partial` are new and nullable, so older files read back as null; the merge reads a
checkpoint from an older image the same way. Each internal contract now carries a description of every table and
column and its quality rules, each naming what enforces it (`contract_docs.py`; a test fails on an undescribed
column). They stay JSON; emitting them as ODCS YAML from `lake.SCHEMAS` is a follow-up.

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
- **A's burst rule**: only a store's first block in a run counts towards a platform-wide pause, and a 430 still
  pauses the platform (section 13).

## 7. Cloud Run Jobs + Cloud Scheduler (no Prefect)

**All crawling moves off GitHub Actions to Cloud Run, not just Shopify.** Every request to a roaster, from
discovery, probing, every connector's crawl and crawl-retry, runs as a Cloud Run Job on Cloud Scheduler. So do the
merge, enrichment and the build + gate + publish. Only Shopify's jobs use the dedicated NAT IP; the rest use default
egress. The Terraform is `src/terraform/ingestion.tf`, and the owner's steps are in `docs/ingestion/RUNBOOK.md`.

Why:
- **One runtime** for the weekly pipeline, with one scheduler, one secrets store (Secret Manager, the app's own
  secrets by name) and one log path (Cloud Logging). Alerting on a failed job is not set up yet: `alerts.tf` covers
  only the app service, so a job-failure alert policy is in Next.
- **Actions minutes saved.** The weekly run took most of its 240 min.
- **Every connector is covered** by the per-connector egress logging and the 8% check, not just Shopify. A bare
  429 or 503 from any adapter counts as a block (`ingest.blocked`), so non-Shopify stores get refusal counts too.
- **Runner IPs aren't shown to be better or worse than other pooled egress.** On the same metric, stores that
  answered any 429: 29% from GitHub-hosted runners (`crawl-plan/backoff.md`) and 37% in A's run 1 from this
  environment's pooled egress (`A/real.md`). (The ~20% in section 2 is another metric: the store's *first* catalog
  request answered 429.) The reasons to move are one runtime and per-connector egress, not the runners' IPs.
- **Cost:** about +$1–2/month for the non-Shopify jobs, and about $4–6/month for the NAT, mostly the static IP.

**Stays on GitHub Actions:** CI, the site deploy and issue-form handling. None of them contacts a roaster.
- **Build + gate + publish moves to Cloud Run** (the `build` job: verdicts, `coffeedata build`, `publish`, then
  `egress-check`). That keeps the weekly pipeline in one runtime, from crawl to the release in the lake.
- **The site deploy stays on Actions**, since GitHub Pages deploys from Actions. At cutover, `site.yml` gets a Sunday
  schedule after the build. It renders the **served** release (it already reads only that), and takes over the
  facts-only public export and the Snowflake load from `weekly.yml`. None of these contacts a roaster.

One image (`ingest.Dockerfile`: the CLI, dbt and the registry seed, pushed to the app's Artifact Registry
repository, built with Cloud Build). Each job's task timeout is 240 min (the strict limit used in every sim).
maxRetries is 0 for every crawl job and 1 for the rest: a crawl that stops at its deadline exits 0 and reports
`deadline` in `job_manifests`, and a second attempt of a failed crawl would only re-ask the stores that just
refused; its resume slot (or crawl-retry) picks up the rest.

| Job | Command | Network | Scheduler (UTC) |
|---|---|---|---|
| `discover` | `discover overture`, `discover osm`, `publish` | default | 1st of the month 03:17 |
| `probe` | `probe --stale-days 28`, `publish` | default | Mon 07:17 |
| `crawl-shopify` | `crawl --connector shopify` (a re-run resumes) | **Direct VPC egress → Cloud NAT, one static IP** | Mon 09:17, resume 15:17 |
| `crawl-retry` | `crawl-retry --connector shopify` | **the same NAT IP** | Mon 21:47 |
| `crawl-squarespace`, `crawl-woocommerce`, `crawl-square-online`, `crawl-generic` | `crawl --connector X` | default | Tue / Wed / Thu / Fri 09:17, resume 15:17 |
| `merge` | `merge` | default | Sun 06:17 |
| `enrich-week` | `enrich-week --submit` | default | Sun 07:17 |
| `build` | `classify`, `build`, `publish`, `egress-check` | default | Sun 09:17 |
| `crawl-shopify-default-egress` | `crawl --connector shopify` | default | never: the confirmation run's control arm |

That is 16 Scheduler entries. They are anchored at the weekly baseline (Monday 09:17 UTC, as `weekly.yml` today),
in B's slots, and `ingest_schedule_enabled` switches all of them off for the confirmation run. Every connector has
its own job, so the others could run on Monday alongside Shopify. That would put a week's fetches closer together
under one run_date. It's a schedule change with no code change; make it if the jobs don't interfere.

**The NAT's ports.** Shopify's jobs send all their traffic through the NAT, the lake reads too: DuckDB reads the hf://
lake with parallel range requests. The default of 64 static ports per VM most likely ran out in the confirmation run's
arm 2 ("Could not connect to server" on its first lake read), so the NAT uses dynamic port allocation, 1,024 to 8,192
ports per VM (endpoint-independent mapping off, as dynamic allocation requires). A job's first lake reads (the
registry state, this run's done and tried roasters, known product counts, blocked hosts) also retry a transient
connection error three times (`lake.retry_transient`, 2/6/18 s), never a missing file or a bad query. Keeping the
lake reads off the NAT would need a split egress (`PRIVATE_RANGES_ONLY` would move the store requests off the dedicated
IP too); not worth it unless ports run out again.

raw/ and parked/ live in a GCS bucket mounted at `COFFEEDATA_STAGING`. They are never published to HF (they aren't
lake tables), so parked progress survives between jobs and the merge empties raw/. Prefect is out for now: the jobs
are containers, so adding it later is a wrapper. Add it only if the dependencies outgrow time-based triggers plus
the manifest table.

**Rollout.** The confirmation run tests Shopify first (section 10). If it passes, Monday's ship moves all connectors
at once. The Actions weekly workflow is then turned off: its schedule is disabled and `workflow_dispatch` is kept,
including the rollback input. It stays in place for one week as a fallback, and a follow-up removes its crawl. The
cutover and rollback steps are in the runbook.

## 8. Pacing (per-connector config, from C)

`connectors.Connector` holds each connector's pacing:
- `rps`: the platform-wide request budget (Shopify: `SHOPIFY_RPS`);
- `shuffle`: stores in a shuffled order, seeded by connector and week, so the same stores aren't always the last
  ones asked (new in this PR);
- `deadline_min`: the crawl window, i.e. how long one job may start new stores, up to its time limit (225 of 240 min).
  It is a cap, not a spread: the main pass starts stores as fast as `rps` allows, and only the deferred rounds are
  spread out. A store still in a page backoff when the window closes is parked for crawl-retry.

"Spread the crawl over hours" is **anecdotal forum advice, not Shopify's** (research §3: Shopify publishes no
numbers). On shared egress, C's spread-out crawl at half the rate showed no drop in 429s: shards 0–4 had 19.4%
first-contact 429s, against 19.6% for A and 22.0% for B. Shards 5–6 dropped to 4.5%, but with a later hour and
longer gaps (section 2), so that is unexplained, not evidence for spreading. On a **dedicated IP**, our own rate
decides that IP's reputation. So the confirmation run tests full speed against a low rate on the dedicated IP
(section 10). If the low rate wins, it's a config change (`rps`), and a main pass spread over the window is the next
thing to test (Next).

## 9. Egress

- **Per connector, in config**: `Connector.egress` is `"dedicated"` for Shopify and `"default"` for the rest. The
  deploy sets the job's network from it, and the job records it in `job_manifests.detail.egress`. It's one design
  with a setting, not a different design per connector. The other connectors stay on default egress unless their
  data shows otherwise.
- **Every run logs each egress IP and its refusals** in `job_manifests`: `egress_ips` lists every IP seen (at start,
  at end, and on blocks), and `refusals[ip, stores, refused]` counts first-contact refusals (429/430 before any page)
  per store, attributed to the IP its first block left from. A store's first contact is its first attempt in the
  run_date: a resume slot or crawl-retry never counts a store an earlier pass of the week already tried. The IP comes from `COFFEEDATA_EGRESS_IP` (a dedicated NAT
  IP is known in advance) or from `COFFEEDATA_EGRESS_ECHO_URL`, a plain "what is my IP" endpoint, never a store. That
  lookup is cached 60 s, since default egress can change IP between connections. With neither set, the IP is
  recorded as unknown.
- **The 8% check**: `coffeedata egress-check` flags an IP with more than 8% first-contact refusals on two production
  runs in a row (a run is a run_date: a main pass and its resume are summed into one; confirmation-run arms are
  left out), and the build job's execution exits non-zero. Nobody is
  emailed yet: there is no alert policy on job failures (Next). There is no automatic action.
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
| 3 | one dedicated NAT IP | low (0.5 req/s); not spread over the window (section 8) |

- **Comparable stores in every arm.** Split all Shopify roasters deterministically: a salted hash of the roaster's
  host into thirds (`crawl --slice k/3 --salt <salt>`), one per arm, so roaster_ids sharing a store stay in one arm.
  (Images before this commit hashed `roaster_id`; with today's salt 15 shared hosts fall in two arms.) Record the
  salt. A repeat draws new thirds with a new salt and a run_date of its own (RUNBOOK §3). `COFFEEDATA_ARM`, the network, the rps and the slice are recorded in
  `job_manifests.detail`. The exact commands are in the runbook.
- **The same conditions.** Lake history present in every arm. The slices are disjoint by store, so only a shared
  egress IP can couple two arms: arms 1 and 2 (different IPs) run at the same time, and arm 3 starts 30 min after
  arm 2 ends (the same NAT IP). Each arm writes to its own `slice=k-of-n` sub-partition, which the merge never reads,
  and an arm's `job_manifests` row never counts as the week's crawl (`control.landed`) or in `egress-check`.
- **Metrics per arm**, from `job_manifests` and `fetches.blocks`:
  - first-contact refusal share per IP, with Wilson 95% intervals;
  - ok share (ok stores over the slice's stores) with Wilson 95% intervals, refused, parked and wall time;
  - the share of blocks that are challenges (`cf_mitigated`) and that carry Retry-After.
- **Decision** (the same rules as RUNBOOK §3; shares, never counts, since the slices differ in size):
  - dedicated egress stays if arm 2's first-contact refusal share is below arm 1's with Wilson intervals that don't
    overlap, or if they overlap while arm 1's blocks are challenges and arm 2's aren't;
  - the low rate becomes the config (`rps`) if arm 3's ok share is above arm 2's with Wilson intervals that don't
    overlap, and arm 3 ended inside 240 min; otherwise full rate stays;
  - if the low rate wins but a whole week's Shopify crawl at that rate can't fit one job's limit, that is the trigger
    to revisit C.

### 10.1 Confirmation run results (2026-11-01)

The arms ran one after another (not arms 1 and 2 together, as planned) on image 7419da12, salt
`confirm-2026-11-01`, run week 2026-11-02. Shares with Wilson 95% intervals:

| | ok share | first-contact refusals | 429/430/503 | challenges | fetch window |
|---|---|---|---|---|---|
| arm 1, default egress, full rate | 349/351 = 99.4% [97.9, 99.8] | 4 = 1.1% [0.4, 2.9] | 0/0/4, all Cloudflare JSON 503s with retry-after, all recovered on retry | 0 | 5.5 min |
| arm 2, dedicated NAT IP, full rate | 363/364 = 99.7% [98.5, 100] | 0 [0, 1.0] | 0/0/0 | 0 | 3.9 min |
| arm 3, dedicated IP, 0.5 rps | 355/355 = 100% [98.9, 100] | 0 [0, 1.1] | 0/0/0 | 0 | 23 min |

Errors: arm 1, javamaster.com (404) and biggunscoffee.com (robots unavailable, closed); arm 2, kaldi.com (page cap).

- **Rule 1 (egress):** dedicated is directionally better, but the intervals overlap, so it isn't statistically
  shown. The arms ran about 2 h apart, so time of day is a confounder.
- **Rule 2 (rate):** the low rate gives no material gain at about 6× the time, so full rate stays.
- **Decision (owner):** keep the dedicated IP for Shopify: directionally better, about $4–6/month, and the stable
  identity that Web Bot Auth and Shopify's higher-limits form need. Every other connector stays on default egress.
- **Infra lessons:**
  - GCSFuse refuses chmod (fixed);
  - Cloud NAT's default of 64 ports per VM was exhausted by DuckDB's parallel HF reads (fixed with dynamic ports,
    1,024–8,192, and connect retries);
  - set `max_retries = 0` for crawl jobs.
- **C (daily shards) stays deferred:** on Cloud Run egress the refusal rate is already about 0–1%.

## 11. Closed and unavailable stores (this PR)

A closed store gets a fetch status of its own, `unavailable`, which is neither `error` nor a block (raw contract
1.2.0 lists the statuses: ok, error, unavailable). `http.Unavailable` is raised on:
- Shopify's closed-store page: a non-200, non-JSON answer to `/products.json` whose text says "This store is
  unavailable" or similar. The same words on a 200 page, or on another platform's page, are not that signature;
- a password-protected storefront (a redirect to `/password`);
- a robots.txt that disallows the whole site, not just the feed. Example: Big Guns, deactivated, 2026-10-31. A
  robots.txt that disallows only `/products.json` is still a robots block (`error`), and so is a robots.txt answered
  with 401 or 403 (often an edge challenge), which is retried, never `unavailable`.

An `unavailable` store:
- is done for the run: resumes and retry passes don't ask it again that week;
- counts as attempted, but `run_completeness` leaves it out of `run_ok_rate` and `adapter_ok_rate`, so closed stores
  don't fail the gate;
- is shown with its last catalog for up to 4 weeks with #63, then drops off.

A 404 or a DNS failure for the whole domain is left as `error` for now. One failed lookup can be transient, so it
should become `unavailable` only after two runs at least a day apart (follow-up 2). Tests: `tests/test_unavailable.py`,
and `test_unavailable_stores_do_not_count_against_the_ok_rates`.

No store in the local lake copy is `unavailable` yet (every fetch there predates the status), so the rule has no
held-out evaluation (CLAUDE.md): that is in Next, once weekly crawls have marked some stores.

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
  - **The dbt `partial` gap, closed in this PR.** `fetches.partial` (nullable, raw 1.2.0) marks an ok fetch that
    read only part of a catalog. dbt lists what it read and keeps the products it didn't read as they were
    (`stg_runs.is_current`). A partial week is never suspect and never enters the baseline. Before this, a secondary
    crawl cut off by a 503 (Coffee King 60 of 74, Artizan 61 of 160) could only be dropped. Tested by the dbt unit
    test `partial_runs_keep_unread_products_listed` and `test_a_partial_fetch_keeps_unread_products_listed`.
    `fct_price_event` reads removals and relistings against complete runs only (`stg_runs.prev_complete_run`,
    `next_complete_run`), so it agrees with `dim_coffee.is_listed` (unit test `price_events_skip_partial_runs`), and a
    week's complete batch wins over a later partial one (`stg_run_walk`). `known_products` ignores partial fetches,
    so a partial read never becomes a store's known size.
  - **Designed and approved for #67 (head `d7a671e`, which holds all of its code), but never built.** These are
    follow-ups (16–18), with no code here:
    - a 503 on `.js` parks `.js` like a 429, and the store continues on its pages;
    - `/collections/all` unioned with the sitemap for every store. Atlas was missing 39 of 41 coffees from its
      sitemap;
    - a store that stops mid-crawl lands as a flagged partial catalog (`partial = true`) instead of an error. That
      needs the `partial` status above.
  - The hook in B: refused stores are labelled in `fetches` and visible in `job_manifests`. The secondary crawler is
    one more connector job that reads them.
- **The Shopify API terms need a legal read.** The Shopify API Terms (updated 2026-10-07) bar using the API "to build
  any commerce or product index" and systematic data collection without the merchant's grant. That rules out
  tokenless Storefront API and Catalog MCP as primary sources on a plain reading, and the Ajax `.js` API is
  documented for themes. #67 uses `.js` per product. Get a legal read before relying on any of them, including
  #67's `.js` path.

## 13. Follow-ups

Done in this PR:
- **Burst rule (A's fix).** Only a store's first block in a run counts towards a platform-wide pause. Retries, page
  backoffs, deferred rounds, parked stores and the retry pass (`crawl-retry`) never count, and a 430 still pauses
  the platform. C's alternative passed known blockers in as `quiet_hosts` for its sweep. A's rule is simpler and
  covers both cases: the in-run repeats, and a retry pass, because `mode="retry_failed"` turns burst counting off.
  A store that blocked in an earlier job of the same run (a resume, crawl-retry) is read from `fetches.blocks` at
  the start, so its block is a repeat too; that replaces C's hand-passed `quiet_hosts`. A's real crawl went from 5
  false platform pauses to 0 with it. **It doesn't fix B's S2 weakness.** B's S2 sim
  (Shopify jobs only, one run each), before and after the fix:
  - parked by the first job: 429 → 468; ok after the first job and its resume: 929 → 927 of 1,070;
  - cooldowns: 17 → 16.

  The S2 losses come from a true IP-wide limit, not false trips. On a dedicated IP that limit is our own rate,
  which the confirmation run's low-rate arm tests.
- **`unavailable` status** (section 11) and **`partial`** (section 12), raw contract 1.2.0.
- **A first-ever run on an empty lake**, through the CLI: crawl → merge → build, with no history and no served
  release (`test_a_first_ever_run_on_an_empty_lake_crawls_merges_and_builds`).
- **Concurrent jobs on one staging bucket** (the confirmation run's arms): each arm compacts only its own slice
  sub-partition, and a publish skips a staged file another job already sent.
- **Cloud Run app service**: the service-level `scaling` block is in `ignore_changes` (a perpetual
  `manual_instance_count 0 -> null` diff on the live service).
- **Enrichment never submits without `--submit`**, even with `ANTHROPIC_API_KEY` and `COFFEEDATA_ANTHROPIC_API_KEY`
  set, via the CLI (`test_enrich_week_never_submits_without_the_flag_even_with_keys_set`).

- **Review fixes before the confirmation run** (PR #68's two reviews): a crawl stopped by its deadline exits 0
  and crawl jobs never retry in place (`max_retries = 0`); a store still in a page backoff at the deadline is
  parked, not dropped; `compact` is idempotent after a kill between write and unlink; arm crawls never land or feed
  `egress-check`; the arms split by host; bare 429/503 `FetchError`s count as blocks; only Shopify's closed-store
  page reads as `unavailable`; `fct_price_event` and `stg_run_walk` handle partial runs. After arm 2's crash on
  its first lake read: NAT dynamic port allocation (1,024–8,192 ports per VM) and a short retry of a job's first
  lake reads on a dropped connection (section 7).
- **Re-review fixes (PR #68):** the RUNBOOK results query filters on salt and run_date and counts each arm's latest
  successful attempt, with a "re-running after a failed arm" procedure; `egress-check` counts a store's first contact
  once per run_date (not per process) and treats a main pass and its resume as one run, so a resume's refusers can't
  raise a false alarm; the `known_bodies` scan and crawl-retry's done query retry a dropped connection too.
- **DevOps review blockers (PR #68):** every job runs one image pinned by digest (`ingest_image`), since Cloud Run
  resolves a tag only when a job is created or updated; the cutover turns the schedule on only after `weekly.yml`'s
  schedule is gone from main and no arm is running; the jobs ignore the `client`/`client_version` a hand `gcloud`
  update stamps.

Next:
1. ~~**Confirmation run**~~: done 2026-11-01 (section 10.1). The dedicated IP stays for Shopify, at full rate.
2. **`unavailable` for a dead domain** (a 404 or DNS failure for the whole domain) after two runs at least a day
   apart.
3. **Under a true IP-wide limit**, count distinct stores blocking together even while they are in page backoff (B's
   S2 weakness). Measure it in the S2 sim, and only if the confirmation run shows an IP-wide limit on the dedicated
   IP.
4. **Refused stores skip to crawl-retry** once refused this week, instead of 3 backoffs per job (7 min each).
5. **Web Bot Auth + the higher-limits form**, once the owner supplies the operator details.
6. **Secondary crawler** (#67) as its own connector job, after a held-out, roaster-grouped score.
7. **The API terms legal read**, before any `.js`, Storefront API or Catalog MCP use.
8. **Conditional requests**: send `If-None-Match` and `If-Modified-Since` where an ETag is known, and log the share
   of 304s.
9. **Lock the control writes**, so two attempts can't race on `job_manifests`.
10. **Same-day connector schedule** (section 7), if the confirmation run shows the jobs don't interfere.
11. **Cutover PR** (RUNBOOK.md): disable `weekly.yml`'s schedule, keeping `workflow_dispatch`, and give `site.yml` a
    Sunday schedule plus the public export and Snowflake load. After one good week, remove `weekly.yml`'s crawl.
12. **Revisit C** only on its trigger ("Deferred: daily shards (C)").
13. **Roaster status as facts (Kimball; dbt review M3).** `dim_roaster` keeps descriptive attributes only: who the roaster is
    (name, site, platform, location). Status is business logic over run-dated outcomes, so it moves to facts
    downstream of the dims:
    - `fct_roaster_run`, grain roaster × run_date: ok, partial or unavailable, products, coffees, and whether the
      catalog was complete and accepted (not failed, truncated, suspect, partial or missing);
    - a status-history model derived from it (`valid_from`, `valid_to`, `is_current` per roaster), where `no_coffee`
      only follows a complete, accepted fetch with 0 coffees;
    - `public_roaster` keeps its public `status` column, sourced from that model instead of the dim, so the public
      contract doesn't change.

    `dim_roaster`'s status logic moves out in that PR, with its own real-lake comparison (it would change
    kaldi.com, steamshovelcoffee.com and sumerce.coffee, section 4).
14. **Internal contracts as ODCS YAML**, generated from `lake.SCHEMAS` and `contract_docs.py` so they can't drift.
15. **Job and Scheduler failure alerts** (before next week; DevOps review N1): a Cloud Monitoring alert on
    `cloud_run_job` failed executions (egress-check, build gate, a crashed crawl) and a log-match alert on Cloud
    Scheduler errors, both to the app's notification channel. Until then nobody is emailed.
16. **#67: a 503 on `.js` parks `.js` like a 429**, and the store continues on its pages.
17. **#67: `/collections/all` unioned with the sitemap** for every store (Atlas: 39 of 41 coffees missing from its
    sitemap).
18. **#67: a store that stops mid-crawl lands as a flagged partial catalog** (`partial = true`) instead of an error.
19. **Main-pass spread** for a low-rate connector: schedule store *i* at `t0 + window·i/n`, as the deferred rounds do,
    so a crawl can actually spread over its window. Only if the confirmation run's low rate wins.
20. **History cost of `int_fetch_product_inputs`** (dbt review M1): classify only each roaster's last ok fetch, as
    `registry.LAST_OK_PRODUCTS_SQL` does, instead of every ELT fetch in history. It grows weekly once ELT crawls land.
21. **`enrich.candidates` efficiency**: push the `keys` filter and the `limit` into SQL and stream rows, instead of
    reading every product version and classifying in Python.
22. **Held-out evaluation of the `unavailable` rule** (CLAUDE.md): count the stores it marks on the real lake and
    check each by hand while there are 50 or fewer; a full split-and-score if more. It waits for data: no store in the
    lake is marked `unavailable` yet.
23. **A merge deletes the arms' slice dirs** (re-review R5): `merge`'s `rmtree(partition)` takes `slice=*/` with it.
    Safe today (arms never land, and the RUNBOOK deletes the slices afterwards); skip `slice=*` subdirectories before
    slices are reused in a production week.
24. **`crawl-retry` has no `--slice`** (R6): an arm's deadline-parked stores could only be resumed into the
    production partition, with no `job_manifests` row. The RUNBOOK forbids crawl-retry during a confirmation run;
    add `--slice` if arm resumes are ever wanted.
25. **`unavailable_reason` on 429/430/503 pages** (R7): a throttling or maintenance page containing "store is
    currently unavailable" reads as closed for the week. The closed-store page is a 402; exclude `BLOCK_STATUSES`
    from `closed_page`.
26. **Big, mostly non-coffee catalogs:** kaldi.com hit the 60-page cap (over 15k products, mostly equipment parts).
    Bound the cost of such catalogs without missing their coffees (for example a coffee collection or a
    product-type filter first, then the cap), measured on held-out stores (CLAUDE.md).

Before next week's scheduled Sunday merge → enrich → build (PR #68's DevOps review; item 15 above is the first):

27. **Chain merge → enrich → build by completion, not fixed times** (N2): `enrich-week` and `build` fail closed unless
    this week's `merge` end row is `done` in `job_manifests` (or one job runs the chain), so a slow merge never
    hands enrichment or the build a half-merged week.
28. **A service account per job class** (N3): only `enrich-week` and `build` can read `ANTHROPIC_API_KEY`; the crawl
    jobs, which parse untrusted roaster HTML, can't.
29. **Ingestion secrets apart from the app's** (N4): `INGEST_HF_TOKEN` (fine-grained to the dataset) and
    `INGEST_ANTHROPIC_API_KEY`, so rotating one after an ingestion incident never breaks the public app.
30. **A budget above the cost estimate** (N6): `budget_usd` is $5 against an estimated $5–8, so its alerts would
    always fire. Raise it (e.g. $20–25) and add a billable-instance-time alert for the jobs.
31. **A dedicated Cloud Build service account** (N7) with only Artifact Registry write and log writing, not the
    default Compute SA.
32. **Base images pinned by digest** (N8): `python:3.12-slim@sha256:…` and `ghcr.io/astral-sh/uv:0.11.32@sha256:…`
    in `ingest.Dockerfile`, with Artifact Registry vulnerability scanning.
