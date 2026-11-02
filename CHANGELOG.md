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
- **Shot-logging app.** Data model and shot logging (#13); sign-in and security headers (#15);
  invites, reset codes, equipment and switching gear (#16); bag capture with Claude reading the label (#17); the
  coffee page, History, Correct and Retract, and `mart_bag` (#18); offline pages and queued photos (#20); layout,
  haptics, dark mode and empty states (#21). Confirmed saves and bag editing (#38). Switching between open bags
  (#53), shots outside the traditional espresso window (#54) and catalog search to correct a bag's coffee (#55).
- **Public site** at registry.coffee: a launch page and runbook (#19), then beta entry points and one visual system
  (#35).
- **Deployment.** The app on Cloud Run with Terraform, JSON logs and alerts (#24), an uptime check (#32), and
  hardening of the single instance against request bursts (#34).
- **Snowflake.** A dry run for `load-snowflake --migrate` (#43).
- **Data licence and citation.** The public dataset is CDLA-Permissive-2.0 (`LICENSE-DATA`); the code stays MIT.
  `CITATION.cff`, and public contract 2.2.2 (documentation only): licence, servers, team, tags and units, with
  the product's input ports (#88).
- **Development.** Pre-commit hooks and ruff formatting (#10). `CLAUDE.md`: heuristic and model changes are
  measured on held-out data (#42), with the effort scaled to the size of the change (#48).

### Changed

- The weekly crawl runs on Cloud Run instead of a GitHub Actions schedule. The `weekly` workflow stays as a manual
  fallback. The crawl runs on Monday, and the `site` workflow publishes the site, the public export and the
  Snowflake load on Tuesday (#74).

### Fixed

- Catalog: parts, cups and test listings dropped; Java and multi-origin titles fixed (#27). Typed coffee names
  crowded out by shared words now match (#57).
- Site: copy fixes (#26). The site workflow renders the served release (#59) and skips while nothing is served
  (#65).
- Issue forms: the add-roaster form reads a state as a code or a full name (#28).
- Lake: a table with no folder on Hugging Face reads as empty (#31), and a remote table is read only once the
  dataset has its folder (#47).
- App: lot ranking, roast-date checks and one read per photo (#44); per-photo upload locks pruned and one UTC rule
  for future roast dates (#50); Save reads "Saving…" until the server replies (#58); fresh pages after writes, no
  duplicate resends and roast-date checks (#66).
- Tests: a date-dependent bag comparison test (#51).

### Security

- App: queued writes are bound to the account that queued them. After an account switch, a queued save is held on
  the phone instead of being written under the other account (#56).
