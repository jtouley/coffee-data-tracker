# Changelog

Notable changes to the code are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the code is versioned with
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each release is a `v`-prefixed git tag, such as
`v0.1.0`, and the version in `pyproject.toml`.

The weekly data releases are versioned separately, by `release_run_date` and `release_build_id` (see the README's
[How it works](README.md#how-it-works)). They aren't code releases and aren't listed here.

## [Unreleased]

These entries are 0.1.0, not tagged yet. When `v0.1.0` is tagged, this heading becomes `## [0.1.0] - YYYY-MM-DD`
and a new `## [Unreleased]` section goes above it.

The first release: the rebuild from PR #9 onward. Earlier history (2024, PRs #1–#5) is the GCP version, at commit
`92f74da`.

### Added

- Anonymous usage counting, page side (the analytics design's PRs 4 and 5): registry.coffee's pages send a
  `page_view`, the catalog its settled `search` and `filter_state`, and every link out to a roaster a `click_out`;
  the app sends its bag-screen catalog search and the coffee page's Shop click. Events wait in memory and leave on a
  click out or when the page is hidden, by `fetch` without credentials to the collector's address in
  `config/usage.toml` (the only new CSP origin, added only once that is set; until then nothing is sent); nothing is
  stored in the browser, no id is made, and nothing is sent with Global Privacy Control or Do Not Track. Links out carry `utm_source=registry.coffee&utm_medium=referral` (`coffeedata/beacon.py`);
  `catalog.json` keeps the roasters' links as they are. The privacy policy says what is counted.
- The crawl success SLO is measured (#96): more than 99% of the known roasters fetched ok in each weekly run, as
  one aggregate over every connector. dbt's `int_crawl_slo_roasters` holds the definition: the scheduled weekly
  runs only (a Monday with a full manifest, or from before manifests), each judged on what it stored (the roasters
  its manifests listed or it tried, plus those earlier runs listed that it gave up on; closed and quarantined
  stores count against it). `mart_crawl_slo` has the share per run and platform with its Wilson 95% interval, the
  burn rate and the error budget over the four runs ending at each run. Both are counted from the stored runs
  alone, so the same raw builds the same rows and an older run rebuilt gives its numbers again. They are report
  only, built after the gate; `mart_crawl_slo_profile` is recorded in the builds table, and the production health
  check warns on it under a new rule, `crawl-slo` (`config/prod_health.toml`; docs/TRIAGE.md, "The crawl success
  SLO"). No gate reads it, and the 90% floor is unchanged. New staging model `stg_run_manifests`, and
  `stg_registry.opted_out_at`.

- Chains leave the public data. A roaster is a chain when it has more than 20 stores (dbt var
  `chain_store_threshold`; the store count is the registry's `locations`, from discovery's Overture scan), unless
  `dbt/seeds/chain_overrides.csv` decides otherwise, either way, with a reason. `int_roaster_state.is_chain` decides
  it; a chain is still crawled and stays in the lake and the internal marts, and `public_roaster` leaves it out, so
  its coffees, price events and market counts leave every public table, the site, the public dataset and the
  shares (`public_market_weekly` no longer counts them). The seed's `is_chain` column is gone: its four curated
  chains whose count is 20 or fewer are overrides now. Public contract 3.1.0 adds the error rule
  `public_tables_have_no_chain`.

- One store, one roaster: a roaster_id whose site resolves to another roaster's store (the same host, any case,
  without www/shop/store; never a parked, shortener, directory or platform host, `dbt/seeds/shared_hosts.csv`) is an
  alias (`dim_roaster.status`, `canonical_roaster_id`), never crawled, never in `dim_coffee`, `fct_price_event` or
  the public tables; a hand-checked `alias_of` in `config/roasters.csv` covers a rebrand that doesn't redirect. The
  grouping never reads the crawl status and the canonical id is sticky (an opt-out, then a publishable id, then the
  one read first), so an outage never splits a store. The rule is written once (`dbt/macros/roaster_alias.sql`),
  for dbt and the crawl. The probe job lists the domains that became aliases. The site's coverage table counts
  aliases on a row of their own. Public contract 2.6.0 adds the error rule
  `public_roasters_unique_by_host`. Measured in `eval/roaster_dedup`.

- Triage alerts (docs/TRIAGE.md). A failed accuracy floor names the eval, the set, the value and the floor (and
  the Wilson interval for a count); on main and in eval-floors it opens or updates one `eval-regression` issue per
  eval and closes it on recovery. `prod-health.yml` judges each served release's per-rule fill and abstain rates,
  week-over-week shift, volume and build record against `config/prod_health.toml`, whose bounds come from the
  release history and carry a severity, and keeps a `prod-health` issue per rule the same way. Alerts never block
  the pipeline.

- Data-quality rules, all warn only and measured on held-out data in `eval/dq`: per-adapter volume bands
  (`adapter_volume_in_band`), near-duplicate roasters, price per 100 g outliers within origin and process, and
  cross-table reconciliation (`public_tables_reconcile`; `release_changes` / `release_changes_explained` against the
  served release, with per-reason release notes printed by `coffeedata build` and recorded in `builds`).

- **Catalog pipeline.** A weekly catalog of US specialty roasters on an immutable Parquet lake, modeled with dbt and
  DuckDB, with roaster discovery from Overture Maps and OpenStreetMap (#9). The catalog is derived in dbt from the
  raw lake, with a measured coffee classifier (#30). Every page of a Shopify catalog is fetched, and coffees are
  marked available, sold out or archived (#33). Square Online stores behind Weebly sites and square.site links are
  read (#11). Wholesale listings stay in the data but out of what people browse (#37). Origins are found by
  context, not by first mention (#41). Ingestion redesigned for Cloud Run: ELT, with one job per connector, a
  weekly build and a dedicated egress IP for Shopify stores; the public contract goes to 2.2.0, and a fetch that
  read only part of a catalog no longer delists or removes a coffee (#68).
- **Crawling etiquette.** One rate limit shared across all Shopify stores (#39). Nothing is fetched from a host
  whose `robots.txt` can't be fetched (#45). Robots rules and rate limits are checked on every redirect hop (#56).
- **Trusted weekly releases.** Run quality and completeness gates, rollback, and ODCS and ODPS contracts (#36).
  Lineage metadata: fetch diagnostics, run manifests, a serving log, the build environment and payload hashes
  (#40). dbt staging tables and unit tests (#49). Run gates calibrated on real data, with per-adapter floors and
  anomaly bands (#52). A warning when the coverage numbers in the listing copy drift from the build (#79).
  Warn-only bands on the filled shares of origin, process, roast level and price, and on the share of the catalog
  seen in the run; invariants, a single release and freshness checked on the public tables, and run-manifest modes
  checked in dbt; public contract 2.3.0 lists the new release rules (#82).
- **Shot-logging app.** Data model and shot logging (#13); sign-in and security headers (#15);
  invites, reset codes, equipment and switching gear (#16); bag capture with Claude reading the label (#17); the
  coffee page, History, Correct and Retract, and `mart_bag` (#18); offline pages and queued photos (#20); layout,
  haptics, dark mode and empty states (#21). Confirmed saves and bag editing (#38). Switching between open bags
  (#53), shots outside the traditional espresso window (#54) and catalog search to correct a bag's coffee (#55).
- **Public site** at registry.coffee: a launch page and runbook (#19), then beta entry points and one visual system
  (#35). Findable: schema.org Dataset JSON-LD, robots.txt and sitemap.xml, only public roasters on the site,
  and self-describing releases (manifest, licence text, `release-<run date>` tags on Hugging Face) (#89).
- **Deployment.** The app on Cloud Run with Terraform, JSON logs and alerts (#24), an uptime check (#32), and
  hardening of the single instance against request bursts (#34).
- **Snowflake.** A dry run for `load-snowflake --migrate` (#43).
- **Data licence and citation.** The public dataset is CDLA-Permissive-2.0
  (`licenses/CDLA-Permissive-2.0.txt`, outside the root so GitHub still shows the code's MIT); the code stays MIT.
  `CITATION.cff`, and public contract 2.2.2 (documentation only): licence, servers, team, tags and units, with
  the product's input ports (#88).
- **Development.** Pre-commit hooks and ruff formatting (#10). `CLAUDE.md`: heuristic and model changes are
  measured on held-out data (#42), with the effort scaled to the size of the change (#48).
- **Fetch health.** `coffeedata fetch-health` checks, shortly after each of Monday's crawl passes and before the
  merge, whether each roaster's fetch returned a catalog of about the right size. It runs the gate's `run_ok_rate`,
  `adapter_ok_rate` and suspect-run rules on the unmerged week and lists the roasters whose fetch failed, came back
  empty or came back far smaller than usual, each with the named gate rule that gave its reason. It judges fetches,
  not data: it doesn't look at prices or at which products came back. Partial fetches and quarantined stores are
  listed apart, never as failures; a challenged store is an ordinary failed fetch, listed with its edge when known
  (SiteGround, Cloudflare). A platform-wide failure, or two at once (the image), emails the owner once a week per
  finding. It never serves or records a build; it publishes only its own job rows, the verdict.
  Re-fetching the listed roasters ships off, behind one setting (`COFFEEDATA_DQ_REFETCH`, unset): off, `crawl` and
  `crawl-retry` never read the list and the week's output is unchanged (0 products). Turning it on would change
  which batch counts for up to 346 products a week, so it needs the full evaluation first
  (`eval/run_quality/README.md`) (#93).

- **Reproducible releases.** Every build records each release table's row count and an order-independent content
  hash (`builds` rows of kind `table_hash`; the per-build `release_build_id` is left out), and
  `coffeedata build-hashes [ID] [--against ID] [--verify]` prints them, compares two builds' and re-hashes a
  release's files, exiting 1 on a difference. `tests/test_reproducible.py` builds a fixture lake twice through
  `coffeedata build`, the second time with every file's rows and every folder's files in reverse order, and
  compares every model table row for row; the second build runs in another time zone, and dbt's session is now
  pinned to UTC (`dbt/profiles.yml`), as production already ran. `docs/ingestion/RUNBOOK.md` §5.6 says how to
  rebuild a served release from raw at its commit and compare its hashes.

### Changed

- **Origin countries are ISO 3166-1.** `origin_country` in dim_coffee and the public tables is the ISO English short
  name, with `origin_country_code` (alpha-2) beside it; the captured spelling stays in the lake and in
  `dim_coffee.origin_country_raw`. A versioned seed (`dbt/seeds/origin_country_iso.csv`, source and version in its
  YAML) maps each captured spelling; a list of several origins, a continent or New Guinea has no country; a new
  spelling is published with no country and warns (`origin_countries_are_mapped`), opening a triage issue. The 2026-11-09 release's 52 market origins for about 46
  countries become 45. Puerto Rico is its own country, "New Guinea" no longer reads as Guinea, and a country inside a
  list of places ("customers in New York, Puerto Rico, and El Paso") is no longer read as the origin. Public contract
  3.0.0. Measured over the full census of 149 spellings in `eval/origin_iso`: all agree with the settled labels, 0
  wrong. dbt also gives each origin a display name (`origin_country_display`): a common name such as Tanzania,
  Vietnam or DR Congo by default, or the ISO name with the dbt var `origin_names: iso`; the site shows it.

- The weekly merge keeps each connector partition it merged for 30 days instead of deleting it: the files it read
  move, unchanged, to `archive/raw/connector=<c>/run_date=<week>/merge=<batch>/` in the staging bucket, where nothing
  else reads, and a lifecycle rule on the bucket deletes `archive/` objects 30 days after the merge. The RUNBOOK
  says how to recover a week from it.
- The weekly crawl runs on Cloud Run instead of a GitHub Actions schedule. The `weekly` workflow stays as a manual
  fallback. The crawl runs on Monday, and the `site` workflow publishes the site, the public export and the
  Snowflake load on Tuesday (#74).
- Crawl hardening (#91). A roaster that kills its crawl (an OOM kill, a segfault, an uncaught error) no longer
  kills every resume: each crawl records the roasters in flight, the next attempt fetches a dead attempt's
  in-flight roasters one at a time, and a roaster in flight in two dead attempts is quarantined for the week with a
  new fetch status, `crashed` (raw contract 1.3.0; left out of the ok rates like `unavailable`; `run_completeness`
  and `int_roaster_runs` gain `quarantined`, and `mart_build_profile` gains `run_quarantined`). Every connector
  gets an evening third pass (Shopify: `crawl-retry` on the NAT IP; the others: their own crawl again, on default
  egress). The non-Shopify crawl jobs retry a failed attempt at once (`max_retries = 1`), and the retry stays
  inside the first attempt's window. `crawl-retry` writes `job_manifests` rows and only resumes its own
  connector's parked stores. `crawl --only <roaster>` fetches one roaster even if it is quarantined. Per-job
  memory overrides (`ingest_job_memory`); `crawl-generic` runs with 8Gi.
- The WooCommerce and generic crawls (`crawl-woocommerce`, `crawl-generic`, every pass) leave from a second
  reserved static IP, separate from Shopify's: SiteGround answers Cloud Run's default egress with a 202
  `sg-captcha` challenge. Terraform adds a second subnet, address and Cloud NAT gateway on the existing router, with
  the Shopify NAT's port settings, and outputs `woo_generic_egress_ip`; Shopify's subnet, IP and NAT are unchanged.
  Each job's egress in `ingestion.tf` is now `default`, `shopify` or `woo_generic` instead of a `nat` flag, and
  `Connector.egress` is `dedicated` for WooCommerce and generic. After apply, the runbook (§2.1) has the owner
  check one SiteGround store from the new IP and open a SiteGround allowlist ticket for it.
- **Snowflake share views have upper-case column names (breaking for queries that quote lower-case names).**
  Marketplace consumers now write `RELEASE_RUN_DATE` (or `release_run_date`) without quotes; before, the views kept
  the tables' quoted lower-case names and needed `"release_run_date"`. Snowflake compares quoted names exactly, so
  once the first load after this change recreates the views, a query that quotes a lower-case name fails with
  `invalid identifier`. Before: `select "release_run_date" from SHARE.COFFEE`. After:
  `select release_run_date from SHARE.COFFEE` (or `RELEASE_RUN_DATE`, or `"RELEASE_RUN_DATE"`). The tables in
  `MARKET` keep their names, and the views keep their descriptions in their definition. Columns are compared in any
  case, and the first load after this change recreates the existing views (`copy grants`) without `--migrate`; a
  view it can't recreate (another role owns it, the share is missing it and the loader can't grant it, or its
  grants can't be read) is left as it is, with a warning on the workflow run, and the data still loads. Each
  recreated view is read back, and the job fails if one is no longer in the share. The
  listing's sample queries are unquoted and upper case: query 3 keeps its `EVENT = 'price'` filter, and query 4, a
  summary by origin, is new.

### Fixed

- **The `discover` and `probe` jobs record new roasters, and say that they ran.** Neither had left anything in the
  lake: `discover` runs monthly and its first slot (Nov 1) came before the schedule was on, so the registry stayed
  empty, and the seed's roasters were all probed 2026-10-26, so the probe found nothing stale (until 2026-11-30)
  and exited 0 without a trace. Now each source (OSM, then Overture) is recorded and published on its own, an
  Overpass outage is bounded and fails the execution without losing Overture's rows, the Overture scan runs in half
  the memory, the probe writes its results in batches as it goes, asks never-probed roasters first and stops at a
  deadline, its publish runs whatever its exit, and both jobs write `job_manifests` rows (jobs `discover`, `probe`).
  RUNBOOK §5.7 says how to run discovery by hand.

- **Claude's coffee-or-not verdicts now fail loudly when they can't run.** They never ran in production: the Terraform
  variable `verdict_model` defaulted to empty, `coffeedata classify` printed a skip message and exited 0, and every
  release was classified by the rules alone (on the sealed set, 10.7% of the products they keep are not coffee,
  against 2.7% with verdicts). `classify` (and `weekly`, before it crawls) now exits non-zero without a verdict model
  or Anthropic credentials, and `classify` also when Claude rejects its batch submit; `--allow-rules-only` or
  `COFFEEDATA_ALLOW_RULES_ONLY=1` is the explicit opt-out for local and dev runs (`make` sets it for a local lake, and
  the tests set it). The build job's chain fails its execution on a failed classify. `dim_coffee` gains
  `classified_by` (`claude` or `rules`) and `verdict_model`, and a new gate, `verdicts_cover_catalog`, fails a build
  with no verdict model or with under 85% of listed coffees decided by a verdict (public contract 2.5.0: the rule and
  its threshold, no column change; #108). `verdict_model` is required in Terraform. RUNBOOK §5.5 covers the gate and
  the backfill.
- Catalog: parts, cups and test listings dropped; Java and multi-origin titles fixed (#27). Typed coffee names
  crowded out by shared words now match (#57).
- Site: copy fixes (#26). The site workflow renders the served release (#59) and skips while nothing is served
  (#65). Without a Hugging Face export the launch page leaves out its "Query it from anywhere" section (the
  example query and the how-it's-built steps beside it) rather than show a placeholder path (#89).
- Issue forms: the add-roaster form reads a state as a code or a full name (#28).
- Lake: a table with no folder on Hugging Face reads as empty (#31), and a remote table is read only once the
  dataset has its folder (#47).
- App: lot ranking, roast-date checks and one read per photo (#44); per-photo upload locks pruned and one UTC rule
  for future roast dates (#50); Save reads "Saving…" until the server replies (#58); fresh pages after writes, no
  duplicate resends and roast-date checks (#66).
- Tests: a date-dependent bag comparison test (#51).
- Builds: every "latest" pick has a total order, so the same lake builds the same tables whatever order DuckDB
  reads rows in. Ties in time are broken by the batch id (the counted batch of a roaster-week, a roaster's last ok
  fetch), by the value picked (probes, registry names, an attempt's adapter, a partial run's reason and a challenged
  run's edge, verdict labels: `coffee` first, so a tie with `coffee` never removes a product, then the label) or by the row's hash
  (enrichments), and the registry's lake sources are listed alphabetically (`string_agg` without an order gave
  different orders on different reads). `mart_dial_in` and `mart_bag` take every `best_*` value from one shot:
  the highest rated, the latest of equal ratings, then `shot_id` (before, equally rated shots gave arbitrary and
  possibly mixed values). `registry.STATE_SQL` breaks ties as `dim_roaster` does. On the real lake (2026-11-04
  copy) no pick had a tie and no roaster had two lake sources, so no catalog output changes; one of four
  coffee-and-grinder groups in the shot log had equally top-rated shots with different recipes.
- Crawl memory (#91): `crawl-generic` was OOM-killed at 2Gi on 2026-11-02. Every httpx response refers to itself
  through its stream, so each page body waited for Python's full garbage collection; the client now breaks that
  cycle once the body is read. A response body is capped at 32 MiB (`COFFEEDATA_MAX_RESPONSE_MB`; a sitemap
  64 MiB, above the protocol's 50 MB): a larger one fails that roaster's fetch instead of the job. Sitemaps are
  matched lazily on their bytes, up to the URL limit.
  An exception from one roaster's fetch is always that roaster's error.

### Security

- App: the sign-in, join, reset and change-password forms post, so a form app.js didn't wire never puts the
  password in the URL, and the sign-in screen's clearing of offline pages can no longer stop the forms being wired.

- App: queued writes are bound to the account that queued them. After an account switch, a queued save is held on
  the phone instead of being written under the other account (#56).
