# Conditional rules: tests and floors

Every rule in the code that decides something about the data (classifies a product, extracts an attribute, filters
a public table, picks a roaster, gates a build), with what pins it. Keep it current: a PR that adds or changes such a
rule adds its row here, with its test and, where it has a labeled eval, its floor (root `CLAUDE.md`, steps 8 and 9).

First drawn from an audit of `main` at `0cfa4d9` (96 rules); every gap that audit listed is closed below. #97 is the
bag-weight rule #136 added afterwards.

## How to read it

- **Test(s):** what pins specific cases: pytest, or a dbt unit test (`dbt/models/unit_tests.yml`, run by
  `tests/test_dbt.py::test_dbt_unit_tests`). A test marked xfail pins a case that looks like a bug (below).
- **Floor:** F = a labeled eval with an asserting floor (table below); M = measured with no floor, and why; – = no
  labeled eval exists for the rule.
- **Runs in CI** (`.github/workflows/ci.yml`):
  - Y: the test job (`make test`).
  - Y(dbt): the test job, building with dbt. The job sets `REQUIRE_DBT=1`, and the dbt-backed tests honour it
    (`tests/lakehelp.py::needs_dbt`), so a missing dbt fails them rather than skipping them.
  - Y(evals): the eval-floors workflow (`.github/workflows/eval-floors.yml`; every push to main, weekly, by hand),
    which rebuilds the floor's private inputs from the lake (below). Never on pull requests.

## Floors

A floor is set at or just below the score measured when it was added, and is only ever raised (a ceiling, for a
rate of confidently wrong answers, only ever lowered). All were measured on 2026-11-11 on `main` at `0cfa4d9`, except bag_grams, which #136 set.

| Floor | Rule | Labels | Inputs | Floors | CI job |
|---|---|---|---|---|---|
| `tests/test_classifier_accuracy.py` | classify.is_coffee | `tests/fixtures/classifier_labels.jsonl` | in git | accuracy train 0.985, validation 0.935 | test |
| `tests/test_bag_grams_accuracy.py` (#136) | bag_weight | `tests/fixtures/bag_grams_labels.jsonl` (eval/bag_grams) | in git | weighted accuracy train 0.999, validation 0.956, the two scored test sets 0.926 and 0.953; weighted wrong at most 0.001, 0.011, 0.0161, 0.0087 | test |
| `tests/test_sealed_accuracy.py` | products.decide over Claude's frozen verdicts; classify.is_coffee | `eval/sealed` (scored once 2026-10-28, now a regression fixture) | in git | stratum-weighted accuracy: decide 0.982, rules 0.940 | test |
| `tests/test_roaster_dedup_accuracy.py` | roaster_canonical (one store, one roaster) | `eval/roaster_dedup/labels` | in git (`config/roasters.csv`) | train 29 and validation 6 same-store pairs called, recall 1.0; 0 different-store pairs called | test |
| `tests/test_wholesale_accuracy.py` | classify.is_wholesale | `eval/wholesale/labels_ref.json` | private: `WHOLESALE_EVAL_DIR` | weighted precision / recall: train 0.97 / 0.85, validation 0.99 / 0.97 | eval-floors workflow |
| `tests/test_origin_accuracy.py` | extract origin_country | `eval/origin/labels_{train,validation}.json` | private: `ORIGIN_EVAL_DIR` | train: weighted agreement 0.959, wrong-country at most 0.040; validation: 97 of its 111 labelled listings agree, at most 11 wrong-country (counts: its sample can't be redrawn, see eval/origin/floor_strata.json) | eval-floors workflow |
| `tests/test_match_accuracy.py` | shots.match_coffee | mechanical (`eval/match/README.md`) | private: `MATCH_EVAL_DIR` | right of 2,000 queries: train 1,621, validation 1,632; confidently wrong at most 2, 3 | eval-floors workflow |
| `tests/test_dq_accuracy.py` | near_duplicate_roasters (#12), price_per_100g_outliers (#7) | `eval/dq/labels` | private: `DQ_EVAL_DIR` | #12: precision and recall 1.0, no distinct pair flagged (both sets); #7 weighted precision 0.857 / 0.957, recall 0.325 / 0.594, false-flag share at most 0.042 / 0.031 (train / validation) | eval-floors workflow |

The test sets stay sealed: the floors score train and validation only (except `eval/sealed`, already scored once).

**Private inputs.** The labels are in git; the listings and catalogs they score are private lake data.
`eval/floor_inputs.py` rebuilds them from the lake (`jtouley/coffee-data`), read-only, at the two revisions the evals
were measured on (`df57f62`, the 2026-10-28 run; `b3bf5e8`, release `20261103T203218-956abd`), and checks each file
against its recorded sha256. The eval-floors workflow (`.github/workflows/eval-floors.yml`) runs it on every push to
main, weekly and by hand, then runs `pytest -m private_eval` with `REQUIRE_EVALS=1`.
- It never runs on pull requests, so no PR branch's code runs with a Hub token; PRs run every floor whose inputs are
  in git (the table's "test" rows) in ci's test job.
- It is not part of ci, which deploy.yml waits on, so a Hub outage can't hold back a deploy.
- No token, a failed download, a changed checksum or a missing input fails the run. Downloads retry network errors
  and 408/429/5xx (backoff with jitter), nothing else. The built inputs are cached by what pins them.
- Token: the `HF_READ_TOKEN` secret if the repository has one (a read-only token scoped to the lake dataset), else
  the `HF_TOKEN` the site and weekly workflows use; only the download step gets it.

Locally, a floor whose folder isn't set is skipped with the reason.

**Not floored, and why:**
- eval/dq #11 (per-adapter volume bands): it scores per-adapter metrics of five replayed builds that never existed
  in the lake (the change added those metrics), so its inputs can't be rebuilt from it. `tests/test_dq_rules.py`
  makes the bands fire on synthetic builds.
- eval/dq #4 (reconciliation): 3 labelled train items, all `by_design`, and no validation item; there is nothing a
  floor could measure. `tests/test_dq_rules.py` makes both checks fire.
- eval/run_quality and eval/crawl: no labels in the repo (running totals only).

## Suspected bugs, pinned as strict xfail

Each test states the output the rule should give, and is a strict xfail with `raises=AssertionError`: it fails the
build once the behaviour changes (the fix then removes the mark), and an unrelated error is never taken for the
expected failure. The dbt ones are unit tests tagged `suspected_bug`, which `test_dbt_unit_tests` leaves out and
`tests/test_dbt.py::test_suspected_bug_unit_tests` runs.

- `tests/test_extract.py::test_a_sun_dried_process_tag_is_natural`: `tag_values` turns `-` into a space, so a
  `process:sun-dried` tag reads `sun dried`, which `_PROCESS`'s `sun-?dried` doesn't match; process is None.
- `tests/test_extract.py::test_a_dot_thousands_separator_is_an_elevation`: "1.800 masl" (a dot as the thousands
  separator) is read as 1.8 m and dropped, not 1,800.
- `tests/test_dbt.py::test_public_market_weekly_leaves_out_chains`: public_market_weekly reads public_coffee, which
  has no is_chain, so it counts a chain's coffees that mart_market and the site leave out.
- dbt unit `flavor_lookup_ignores_case`: the flavor lexicon lookup is exact, so a capitalised note gets no category.
- dbt unit `a_bag_with_no_shots_has_none_in_window`: mart_bag gives a bag with no shots `shots_in_window` null, while
  its shot_count is 0.

## Inventory

| # | Rule | file:line | What it decides | Test(s) | Floor | Runs in CI |
|---|---|---|---|---|---|---|
| **Classification and exclusion** | | | | | | |
| 1 | `classify.excluded`: gear, merch, tea, parts, part numbers and part sellers, test listings, filters, subscriptions, green coffee, type and tag metadata | src/coffeedata/classify.py:137 | Never coffee, and skips Claude | test_classify.py (14 tests, many cases) | F classifier (train 0.985, validation 0.935); F sealed set, rules (0.940) | Y |
| 2 | `classify.is_coffee`: sauce, sweets and drinks guards, coffee words, countries, demonyms and coffee type; else 2 of 4 signals | src/coffeedata/classify.py:161 | Coffee or not (rules) | test_classify.py | F classifier (train 0.985, validation 0.935); F sealed set, rules (0.940) | Y |
| 3 | `classify.is_wholesale` / `says_wholesale`, plus tag full-match | src/coffeedata/classify.py:125 | Wholesale flag: out of price/100 g, market, counts and browse | test_classify.py::test_wholesale_patterns_found_on_the_real_catalog, test_green_…; test_dbt wholesale twins | F wholesale (train P 0.97 / R 0.85, validation P 0.99 / R 0.97) | Cases Y; floor Y(evals) |
| 4 | `products.decide`: Claude verdict wins unless `excluded` | src/coffeedata/products.py:41 | is_coffee and classified_by | test_classify.py::test_decide_never_lets_a_verdict_overrule_an_exclusion, ::test_decide_takes_claudes_coffee_or_not_coffee_and_the_rules_otherwise; test_verdict.py::test_the_build_takes_claudes_verdict_over_the_rules | F sealed set, decide over Claude's frozen verdicts (0.982) | Y |
| 5 | stg_verdicts: latest verdict for the model and definition, tie prefers coffee | dbt/models/staging/stg_verdicts.sql:9 | Which verdict counts | dbt unit `verdict_tie_prefers_coffee` (test_dbt::test_dbt_unit_tests) | – | Y(dbt) |
| 6 | `verdict.pending`: skips rule exclusions, answered and in-flight versions | src/coffeedata/verdict.py:54 | What is sent to Claude | test_verdict.py::test_pending_skips_… | – | Y |
| 7 | `verdict.parse`: only a known label | src/coffeedata/verdict.py:159 | Verdict stored or dropped | test_verdict.py::test_parse_takes_only_a_known_label | – | Y |
| 8 | Verdict gate (`min_verdict_share` 0.85, require_verdicts) | dbt/tests/verdicts_cover_catalog.sql | Build fails when too few verdicts | test_verdict.py::test_the_verdict_gate_fails_… | – | Y(dbt) |
| 9 | `int_fetch_coffees` / `registry.connect_for_state`: rules-only coffee count per ok fetch | dbt/models/intermediate/int_fetch_coffees.py; src/coffeedata/registry.py:245 | Roaster `active` vs `no_coffee` | test_dbt.py::test_roaster_status_reads_the_recorded_coffee_count_else_counts_it | – | Y(dbt) |
| 10 | Python and dbt tag readers agree | dbt/tests/python_readers_agree_on_tags.sql | Build error when decoding flips a rule | test_dbt.py::test_the_build_fails_when_raw_and_decoded_tags_decide_differently | – | Y(dbt) |
| 11 | HTML-entity decoding of tags in dbt | dbt/macros/html_entities.sql:22 | Tags the rules read | test_html_entities.py; dbt unit `snapshot_tags_are_decoded` | – | Y |
| 12 | `CatalogItem._unescape_title` (unescape up to 3 times, strip markup) | src/coffeedata/models.py:86 | clean_title | test_models.py::test_double_escaped…, test_markup… | – | Y |
| **Bag weight and price** | | | | | | |
| 13 | `models.parse_grams`: kg/lb/oz/g, range 30–25,000 g | src/coffeedata/models.py:231 | The grams the crawler stores; since #136 the build reads them again (#97) | test_models.py::test_parse_grams (10 cases) | – | Y |
| 14 | dim_coffee price_per_100g: not wholesale, 150–2,500 g, price > 0, available, $0.50–$100/100 g | dbt/models/marts/dim_coffee.sql:50 | Public price_per_100g (25,256 coffees) | dbt unit `price_per_100g_band_size_window_and_availability` (band, size window, both bounds, sold out, unknown availability, wholesale); test_dq_rules outliers | – | Y(dbt) |
| 15 | dim_coffee min_price_usd (`price_usd > 0`), bag_sizes_g | dbt/models/marts/dim_coffee.sql:47 | Public min price (42,781) and sizes | dbt unit `price_per_100g_band_size_window_and_availability` (min_price_usd > 0 over every variant, bag_sizes_g); test_currency.py | – | Y(dbt) |
| 16 | dim_coffee catalog_status: delisted, preorder (ship-date passed → available), available, archived, sold_out | dbt/models/marts/dim_coffee.sql:83 | Public catalog_status; site and market counts | test_dbt.py::test_catalog_status_separates_current_from_back_catalog | – | Y(dbt) |
| 17 | dim_coffee in_stock (is_current and any available) | dbt/models/marts/dim_coffee.sql:80 | Public in_stock (38,536) | test_dbt.py history and catalog status tests | – | Y(dbt) |
| 18 | dim_coffee safe url (`https?://` only) | dbt/models/marts/dim_coffee.sql:70 | Coffee published only with a safe link | test_dbt.py::test_safe_url (site), test_public_tables_are_facts_only… | – | Y(dbt) |
| 19 | Claude extraction overrides rules attributes | dbt/models/marts/dim_coffee.sql:24 | attributes_source | test_dbt.py::test_claudes_extraction_still_overrides_the_rules | – | Y(dbt) |
| 20 | flavor_categories via flavor_lexicon seed | dbt/models/marts/dim_coffee.sql:63 | Public flavor_categories (8,846 coffees) | dbt unit `flavor_categories_come_from_the_lexicon`; xfail `flavor_lookup_ignores_case` | – | Y(dbt) |
| **Currency and country** | | | | | | |
| 21 | `is_usd` macro (USD stated, or none and roaster in US) | dbt/macros/currency.sql:3 | Which prices publish as USD | test_currency.py (US-defaulted, CAD, cross-currency) | – | Y(dbt) |
| 22 | stg_snapshots: `price_usd` null unless is_usd; variant_id de-dup `#i` | dbt/models/staging/stg_snapshots.sql:16 | Prices and variant keys | test_currency.py; test_dbt.py::test_repeated_variant_ids_in_raw_snapshots | – | Y(dbt) |
| 23 | stg_registry country default 'US'; `is_publishable` (overture/curated/manual and US) | dbt/models/staging/stg_registry.sql:29; dbt/macros/roaster_alias.sql:22 | Public roasters | test_currency.py::test_public_tables_publish_usd_prices_and_us_roasters_only; test_dbt.py::test_public_tables_are_facts_only_and_filtered (OSM-only, opted out) | – | Y(dbt) |
| 24 | Adapter currency readers (Shopify cookie/page, Woo `_currency`, Squarespace, Square store, JSON-LD offer) | src/coffeedata/adapters/*.py | Stored currency | test_currency.py (14 tests) | – | Y |
| 25 | `public_prices_are_usd` quality rule | dbt/tests/public_prices_are_usd.sql | Build error on a non-USD public price | test_currency.py | – | Y(dbt) |
| **Origin and process extraction (rules attributes: 42,625 coffees)** | | | | | | |
| 26 | `extract.origin_country` plus `_strip_false_places`, Java, `_NO_ORIGIN`, multi-country title, regions, `_title_is_variety` | src/coffeedata/extract.py:287 | origin_country (21,510 rules coffees) | test_extract.py (70 probe cases + 6 tests) | F origin (train weighted agreement 0.959, wrong-country at most 0.040; validation 97 of 111 agree, at most 11 wrong-country) | Cases Y; floor Y(evals) |
| 27 | `origins.canonical_country` aliases (Sumatra→Indonesia, Kona→US, Congo→DRC, PNG misspellings→Papua New Guinea; Puerto Rico and New Guinea stay themselves) | src/coffeedata/origins.py:82 | Country names | test_extract.py::test_canonical_country_aliases (every alias), ::test_new_guinea_and_puerto_rico_titles | – | Y |
| 28 | `_process` (anaerobic > honey > natural > washed), from tag, title, then "Process:" | src/coffeedata/extract.py:311 | process (7,839 rules coffees) | test_extract.py::test_process (natural, dry, semi-washed, wet-hulled, precedence within a text and across tag/title/body); xfail test_a_sun_dried_process_tag_is_natural | – | Y |
| 29 | `_ROAST` (light/dark/medium patterns, roast tags) | src/coffeedata/extract.py:23 | roast_level (16,654 rules coffees) | test_extract.py::test_roast_level (light, nordic, dark, French, Italian, city, body patterns, tags, light-before-dark order) | – | Y |
| 30 | `is_decaf` (`\bdecaf` in title or tags, caffeine tag) | src/coffeedata/extract.py:358 | is_decaf (4,478 rules / 4,920 all) | test_extract.py::test_is_decaf, ::test_is_decaf_reads_the_title_and_tags_not_the_body | – | Y |
| 31 | `is_blend` (blend in title, Mocha Java, origin tag) and blend clears the body origin | src/coffeedata/extract.py:360 | is_blend (6,464 rules); market exclusion | test_extract.py (Mocha Java, `origin__blend`) | – | Y |
| 32 | Varietal spellings | src/coffeedata/extract.py:66 | varietals (8,290) | test_extract.py::test_varietal_spellings | – | Y |
| 33 | `_MASL` / `_int` (300–3,000 m) | src/coffeedata/extract.py:108 | elevation (6,273) | test_extract.py::test_elevation_range_clamp; xfail test_a_dot_thousands_separator_is_an_elevation | – | Y |
| 34 | Tasting notes: `split_notes`, `tag_values`, `_NOTES` | src/coffeedata/extract.py:132 | tasting_notes (7,875) | test_extract.py (2 cases) | – | Y |
| 35 | `needs_llm` and enrich candidates | src/coffeedata/extract.py:368; src/coffeedata/enrich.py:27 | What Claude enriches | test_extract.py::test_needs_llm; test_enrich.py::test_pending_is_derived… | – | Y |
| 36 | stg_enrichments pick (llm first, latest, md5 tiebreak) | dbt/models/staging/stg_enrichments.sql:7 | Which enrichment counts | test_dbt.py / test_reproducible.py (indirect) | – | Y(dbt) |
| **Registry status and aliases** | | | | | | |
| 37 | Status CASE: opted_out, unreachable (3 failed runs, newer probe), unreachable (no or `http_*` platform), pending, active, no_coffee, no_catalog | src/coffeedata/registry.py:180; dbt/models/intermediate/int_roaster_state.sql:41 | Crawlability and public status | test_dbt.py::test_python_and_dbt_agree_on_no_catalog_and_platformless_roasters (no_catalog, http_* and no platform, Python and dbt) | – | Y(dbt) |
| 38 | Probe `robots_unavailable` isn't a probe; a failed probe never erases the platform | src/coffeedata/registry.py:153 | last_probed and platform | test_registry.py::test_a_probe_without_robots_txt…, test_state_from_history | – | Y |
| 39 | `normalize_domain` / `host_key` (Python and SQL) | src/coffeedata/registry.py:58; dbt/macros/roaster_alias.sql:10 | One store key | test_registry.py (normalize, host_key, SQL equals Python) | – | Y |
| 40 | `is_shared_host`, NOT_A_ROASTER_SITE, `is_storefront_host` | dbt/macros/roaster_alias.sql:17; src/coffeedata/registry.py:83 | Never group; never a roaster | test_registry.py::test_shared_hosts…, test_storefront_hosts; test_crawl_fix_round.py::test_18 | – | Y |
| 41 | `roaster_canonical` ordering (opted-out, publishable, first catalog, id = key, label, curated, earliest, name; alias_of) | dbt/macros/roaster_alias.sql:45 | Canonical vs alias (993 coffees moved) | dbt units `one_store_one_canonical_roaster`, `a_new_domain_never_takes_over…`, `a_store_keeps_its_aliases…`; test_dbt (3 tests); test_registry (alias never crawled, dropped never groups) | F roaster_dedup (train 29/29, validation 6/6 same-store pairs called; 0 different-store pairs called) | Y |
| 42 | Alias exclusion from dim_coffee / fct_price_event; canonical_coffee_id remap for shots and bags | dbt/models/marts/dim_coffee.sql:109; dbt/macros/canonical_coffee_id.sql | Public rows; shot links | test_dbt.py::test_one_store_is_one_roaster…; quality tests `roaster_aliases_resolve`, `public_roasters_unique_by_host`, `stranded_alias_stores` | – | Y(dbt) |
| 43 | `registry.same_store` (probe reports new aliases) | src/coffeedata/registry.py:267 | Probe job detail | test_registry.py::test_the_probe_names_domains_that_became_aliases | – | Y |
| 44 | Unavailable / closed: `unavailable_reason` (password redirect, Shopify closed page), `SiteDisallowed` | src/coffeedata/http.py:266 | Fetch status `unavailable` | test_unavailable.py (4 tests); test_challenges.py | – | Y |
| 45 | dim_roaster counts (listed: not archived and not wholesale) | dbt/models/marts/dim_roaster.sql:7 | coffees_listed / in_stock | test_dbt.py::test_wholesale_twins_count_in_no_weekly_or_roaster_number | – | Y(dbt) |
| **Crawl: challenges, 429 and partial handling** | | | | | | |
| 46 | `is_challenge` / `challenged` (202, `sg-captcha`, `cf-mitigated`) | src/coffeedata/http.py:186 | Refusal, never deferred | test_challenges.py; test_block_headers.py; test_crawl_fix_round.py | – | Y |
| 47 | `ingest.blocked`, `deferrable`, `block_wait`, `server_paused` (429/430/503, 5xx with long Retry-After, timeouts) | src/coffeedata/ingest.py:72 | Defer vs fail a store | test_rate_limited_stores.py, test_ingest.py, test_challenges.py, test_ratelimit.py (5xx Retry-After cases in test_rate_limited_stores:321) | – | Y |
| 48 | `retry_after_seconds` / `asked_wait` / `store_wait` cap (MAX_STORE_WAIT) | src/coffeedata/http.py:117 | Wait length | test_ratelimit.py; test_crawl_fix_round.py (RateLimit-Retry-After) | – | Y |
| 49 | Shopify hard block (401/403 StoreBlocked), page block and backoff (PAGE_BACKOFFS), parking | src/coffeedata/adapters/shopify.py:84; src/coffeedata/ingest.py:69 | Store error vs parked | test_adapters.py::test_shopify_403_raises; test_rate_limited_stores.py; test_ratelimit.py | – | Y |
| 50 | PlatformLimiter (Shopify 3 rps / 8 concurrent, Squarespace 2 rps), host pause | src/coffeedata/ratelimit.py:69 | Request pacing | test_ratelimit.py, test_burst.py | – | Y |
| 51 | Partial catalog: PartialCatalog with items → ok+partial, none → error; generic `_partial_reason` | src/coffeedata/ingest.py:196; src/coffeedata/adapters/generic.py:140 | Partial vs error | test_catalog_failures.py, test_crawl_caps.py, test_crawl_fix_round.py | – | Y |
| 52 | Shopify short page ends catalog only at known size (`known_products`) | src/coffeedata/adapters/shopify.py:43 | Catalog completeness | test_adapters.py::test_shopify_short_pages… | – | Y |
| 53 | `partial_every_week` (PARTIAL_WEEKS = 2) | src/coffeedata/ingest.py:260 | Warning | test_crawl_fix_round.py | – | Y |
| 54 | `select_states` / `_in_pool` / `crawlable` / `_done_and_tried` / `select_parked` | src/coffeedata/ingest.py:300 | Who is crawled | test_registry.py (alias never crawled), test_rate_limited_stores.py, test_lake.py, test_ingest.py | – | Y |
| 55 | robots.txt: `can_fetch`, crawl-delay (MAX_CRAWL_DELAY), whole-site disallow | src/coffeedata/http.py:476 | Fetch allowed | test_robots_identity.py; test_adapters.py::test_robots_disallow… | – | Y |
| 56 | Response size caps (MAX_RESPONSE_BYTES, SITEMAP_MAX_BYTES) | src/coffeedata/http.py:66 | Fetch error | test_crashguard.py (5 tests) | – | Y |
| 57 | crashguard quarantine (QUARANTINE_AFTER = 2 dead attempts) | src/coffeedata/crashguard.py:99 | Status `crashed` | test_crashguard.py::test_a_roaster_that_kills_two_attempts… | – | Y |
| 58 | Woo bulk variation prices (BULK_REJECTED 400/403, one-by-one fallback), `_size_label` | src/coffeedata/adapters/woocommerce.py:73 | Variant prices and grams | test_adapters.py; test_crawl_fix_round.py; test_crawl_caps.py | – | Y |
| 59 | Generic adapter `_PRODUCTISH` / `_BEANY` / `_MERCHY` URL ranking; JSON-LD parse | src/coffeedata/adapters/generic.py:26 | Which URLs are read | test_adapters.py::test_product_urls_rank…, test_generic_* | – | Y |
| 60 | Square Online ids, store links; Squarespace shop paths; sitemap `_refusal` | src/coffeedata/adapters/square_online.py:43; src/coffeedata/sitemap.py:45 | Catalog discovery | test_adapters.py (8 square tests); test_crashguard sitemap; test_challenges | – | Y |
| **Runs, fetch-health and gate thresholds** | | | | | | |
| 61 | int_roaster_runs: ok, unavailable, quarantined, partial, partial_reason, challenge per roaster-week | dbt/models/intermediate/int_roaster_runs.sql:10 | Run states | dbt unit `roaster_runs_carry_partial_and_challenge` | – | Y(dbt) |
| 62 | stg_fetches challenge (siteground/cloudflare from blocks) | dbt/models/staging/stg_fetches.sql:11 | challenge column | dbt unit above; test_fetchhealth.py | – | Y(dbt) |
| 63 | stg_run_walk counted batch: override reject/accept/use_batch, partial counted, suspect not; batch pick order | dbt/models/staging/stg_run_walk.sql:20 | Which batch counts | test_quality.py (overrides ×5, partial weeks); dbt unit `suspect_run_boundaries` | – | Y(dbt) |
| 64 | `suspect_run` (< 0.5 × median of last 4, median ≥ 20) | dbt/macros/suspect_run.sql:7 | Suspect weeks | dbt unit `suspect_run_boundaries`; test_quality.py::test_a_truncated_week… | – | Y(dbt) |
| 65 | stg_runs prev/next complete run, is_current, is_latest | dbt/models/staging/stg_runs.sql:5 | Listing and removal | dbt units `run_sequence_skips_partial…`, `partial_runs_keep_unread_products_listed` | – | Y(dbt) |
| 66 | run_completeness expected set (manifest vs reconstructed), has_catalog CASE, catalog_ok, adapter fallback | dbt/models/marts/run_completeness.sql:50 | Gate population | dbt units `unavailable_…`, `quarantined_…`; test_quality.py (completeness ×4) | – | Y(dbt) |
| 67 | Gates: run_ok_rate (min_ok_share 0.9), adapter_ok_rate (0.5, ≥ 20 roasters), run_is_complete (1.0), latest_run_is_fresh (8 days), catalog_volume_vs_last_build (0.2), public_coffee_is_fresh | dbt/tests/*.sql; dbt/dbt_project.yml vars | Build fails and the release isn't served | test_quality.py (≈ 15 tests); test_fetchhealth.py | – | Y(dbt) |
| 68 | `fetchhealth.judge`: failed, empty or suspect reasons; systemic connector, image (≥ 2) and run findings; held back; unattributed | src/coffeedata/fetchhealth.py:184 | Repull list and findings | test_fetchhealth.py (33 tests) | M (eval/run_quality, no labels) | Y |
| 69 | `refetch_enabled`, `repull_states` | src/coffeedata/fetchhealth.py:131 | Re-fetch gate (off) | test_fetchhealth.py | – | Y |
| 70 | Anomaly bands `metrics_out_of_band` (k 1.5 × MAD, min half-widths, ≥ 6 builds, resets) | dbt/macros/metrics_out_of_band.sql:9 | Warnings | test_quality.py (bands ×4, filled shares); test_dq_rules.py (adapter bands) | M (eval/dq #11): not floored, see Floors | Y(dbt) |
| 71 | stg_builds `passed`; baselines | dbt/models/staging/stg_builds.sql:1 | Which builds are baselines | test_quality.py::test_dbt_and_quality_builds_agree…, test_a_build_with_no_pass_verdict… | – | Y(dbt) |
| **dbt mart CASE and filter logic** | | | | | | |
| 72 | `price_event` CASE (new, relisted, price (USD to USD), availability) and `removed` at next complete run | dbt/macros/price_event.sql:11; dbt/models/marts/fct_price_event.sql:42 | Public price events | dbt units `price_event_types`, `price_events_skip_partial_runs`; test_dbt::test_relisting; test_currency.py | – | Y(dbt) |
| 73 | public_roaster filter (not opted out, has coffee, publishable, US) | dbt/models/public/public_roaster.sql:17 | Public roasters | test_dbt.py::test_public_tables_are_facts_only_and_filtered; test_currency.py | – | Y(dbt) |
| 74 | public_coffee (`url is not null`) / public_price_event (public coffees only) | dbt/models/public/public_coffee.sql:16 | Public rows | test_dbt.py; test_dq_rules reconcile | – | Y(dbt) |
| 75 | public_market_weekly filter (listed, not archived, not wholesale, not blend, origin known) | dbt/models/public/public_market_weekly.sql:2 | Public market table (183 to 194 cells) | dbt unit `market_weekly_cells_leave_out_archived_blends_and_unknown_origins` (archived, blend, wholesale, no origin, `unknown` process); xfail test_dbt.py::test_public_market_weekly_leaves_out_chains | – | Y(dbt) |
| 76 | mart_market filter (also not chain, US) and top-5 notes per origin | dbt/models/marts/mart_market.sql:6, :12 | Served mart and site | dbt unit `mart_market_is_us_retail_single_origins_without_chains` (chain, non-US, wholesale, blend, no origin, top-5 notes) | – | Y(dbt) |
| 77 | `is_chain` exclusions (mart_market, site catalog, origin price, reach) | dbt/models/marts/mart_market.sql:3; src/coffeedata/site.py:287 | Site and market counts (2 chains, 149 coffees) | test_dbt.py::test_a_chains_coffees_leave_the_market_and_the_site_catalog; dbt unit `mart_market_is_us_retail_single_origins_without_chains` | – | Y(dbt) |
| 78 | mart_new_arrivals (first_seen = latest run, prev run exists, published since prev run, in stock, retail) | dbt/models/marts/mart_new_arrivals.sql:8 | New-arrivals list | test_dbt.py::test_new_arrivals_are_newly_published_not_newly_fetched | – | Y(dbt) |
| 79 | Site aggregates (public roasters only, retail, ≥ 5 per origin) | src/coffeedata/site.py:281 | Site numbers | test_dbt.py::test_site_shows_only_the_public_roasters, wholesale twins | – | Y(dbt) |
| 80 | MCP / app catalog search `retail_only`, filters | src/coffeedata/shots.py:66; src/coffeedata/mcp_server.py:64 | Search results | test_mcp.py; test_bags.py | – | Y |
| 81 | fct_shot extraction_time_bucket (< 25 fast, ≤ 32 target, else slow), brew_ratio, grind_position | dbt/models/marts/fct_shot.sql:19 | Personal shot marts (not public) | dbt unit `extraction_time_bucket_boundaries` (25 and 32 s bounds, brew_ratio), `shots_in_window_counts_target_shots_per_bag` (mart_bag); xfail `a_bag_with_no_shots_has_none_in_window` | – | Y(dbt) |
| 82 | mart_dial_in best recipe; mart_bag best shot | dbt/models/marts/mart_dial_in.sql:16 | Personal | dbt unit `dial_in_best_recipe_is_one_shot…`; test_dbt shots | – | Y(dbt) |
| 83 | stg_shots latest, not deleted; stg_run_overrides tiebreak | dbt/models/staging/stg_shots.sql:7; stg_run_overrides.sql:6 | Dedupe | test_coffee/test_shots retractions; test_quality::test_overrides_given_in_the_same_second… | – | Y(dbt) |
| 84 | `shots.match_coffee` / `_confident` (80 and 5 clear; candidates 50) | src/coffeedata/shots.py:194 | Bag and shot links | test_bags.py (≈ 12 match tests); test_shots.py | F match (right 1,621 / 1,632 of 2,000; confidently wrong at most 2 / 3) | Cases Y; floor Y(evals) |
| 85 | `suggest_next` (target ratio and time) | src/coffeedata/shots.py:418 | Advice | test_shots.py::test_suggest_next, test_balanced_but_slow… | – | Y |
| **Contract quality rules (dbt tests named in the contracts)** | | | | | | |
| 86 | public_tables_invariants, public_tables_are_one_release, run_manifest_mode_values | dbt/tests/*.sql | Release integrity | test_quality.py (made to fail) | – | Y(dbt) |
| 87 | public_tables_are_facts_only | dbt/tests/public_tables_are_facts_only.sql | Column guardrail | test_dbt.py::test_each_pass_path_guard_fails_on_the_row_it_guards_against (an exposed `tags` column) | – | Y(dbt) |
| 88 | coverage claims (claim_tolerance 0.15; unknown metric fails) | dbt/tests/coverage_claims_*.sql; src/coffeedata/quality.py:372 | Marketing claims | test_quality.py (4 tests) | – | Y(dbt) |
| 89 | Raw contract rules (ingest, enrichment and control rows follow the contract) | contracts/internal/*.odcs.yaml | Schema | test_raw_contracts.py | – | Y |
| 90 | DQ warnings: near_duplicate_roasters, price_per_100g_outliers (z 3/6, group ≥ 30), public_tables_reconcile, release_changes_explained | dbt/tests/*.sql | Warnings | test_dq_rules.py (made to fire) | F dq #12 near_duplicate_roasters (precision and recall 1.0, 0 distinct flagged) and #7 price_per_100g_outliers (weighted precision 0.857 / 0.957, recall 0.325 / 0.594, false-flag share at most 0.042 / 0.031); #4 not floored, see Floors | Y(dbt); floor Y(evals) |
| 91 | price_per_100g_in_range, sane_coffee_listings, unique_runs | dbt/tests/*.sql | Error-severity guards | test_dbt.py::test_each_pass_path_guard_fails_on_the_row_it_guards_against (each made to fail) | – | Y(dbt) |
| **Discovery filters** | | | | | | |
| 92 | Overture SQL filter: US bboxes, `country = 'US'`, website present, coffee_roastery or "roast" + coffee | src/coffeedata/discover.py:144 | Which places enter the registry | test_discover_probe_jobs.py::test_overture_filter_keeps_us_roasteries_with_a_storefront (the scan's SQL over a local file: boxes, country, website, tag, alternates, name and category, storefront hosts, one row per domain) | – | Y |
| 93 | OSM `craft=coffee_roaster` + storefront host; outage after 3 states | src/coffeedata/discover.py:223 | Registry additions | test_discover_probe_jobs.py::test_osm_keeps_storefront_websites_once; outage and budget in test_discover_probe_jobs.py | – | Y |
| 94 | `probe_one` platform detection: JSON endpoints (shopify/woo/squarespace…) then HTML_MARKERS (`shopify?` …), `http_<code>`, blocked, robots_unavailable | src/coffeedata/discover.py:282 | Platform, so the adapter and whether the roaster is crawled | test_discover_probe_jobs.py::test_probe_detects_the_platform (JSON endpoints, every HTML marker, order, other), ::test_probe_reports_a_non_200_home_page_as_http_status, ::test_probe_tries_www_…, ::test_probe_with_no_host_answering_is_unreachable; test_crawl_fix_round.py::test_19 | – | Y |
| 95 | `state_code` (names and codes, DC/PR) | src/coffeedata/discover.py:103 | Roaster state | test_registry.py::test_state_codes_from_the_issue_form | – | Y |
| 96 | Probe staleness and order (never probed first, then oldest) | src/coffeedata/discover.py:342 / cli | What is probed | test_discover_probe_jobs.py | – | Y |
| 97 | `bag_weight` via `stg_variant_grams`: each option's grams read again at build time from its stored title, then the product title or description; misreads corrected, packs totalled, bundles and weights the listing never states dropped, 30 g–25 kg; joined to dim_coffee by option position, a version with no row keeps its stored grams | src/coffeedata/bag_weight.py; dbt/models/staging/stg_variant_grams.py; dbt/models/marts/dim_coffee.sql:14 | Variant grams, so price_per_100g and bag sizes (added by #136) | test_bag_weight.py; test_dbt.py::test_bag_weights_are_read_again_from_the_stored_text; dbt unit `coffee_prices_use_the_rules_grams_by_position`; guard `variant_grams_are_valid` made to fail in test_dbt.py::test_each_pass_path_guard_fails_on_the_row_it_guards_against | F bag_grams (`tests/test_bag_grams_accuracy.py`, from #136) | Y(dbt) |
| 98 | Origin spelling → ISO 3166-1: the `origin_country_iso` seed (versioned; source in its YAML meta) and `origin_iso` macro (whole spelling, else a list read part by part: one country, `multiple`, or `unmapped`), published as dim_coffee / public `origin_country` (ISO short name) and `origin_country_code`; `origin_countries_are_mapped` (error) fails a build on an unmapped spelling, `origin_country_iso_matches_iso` (error) checks the seed against ISO | dbt/macros/origin_iso.sql; dbt/models/intermediate/int_origin_country.sql; dbt/models/marts/dim_coffee.sql:85 | origin_country of every coffee (643 changed against main 0879254) and public_market_weekly's cells | dbt unit `origin_spellings_resolve_to_iso_3166_1`; test_dbt.py::test_dim_coffee_maps_origins_to_iso (and the unmapped gate failing); test_mcp.py::test_search_finds_an_origin_by_its_iso_name_spelling_or_code | Pending: the census of all 149 captured spellings is being labeled (eval/origin_iso); the floor test lands with the labels | Y(dbt) |

### Counts (98 rules)

| | Rules |
|---|---|
| Case-tested | 98 (every partial, pass-path-only and untested rule of the audit now has a case test) |
| With an asserting floor | 9 rules under 8 floors: #97 (bag_grams); #1–2 (classifier, sealed rules), #3 (wholesale), #4 (sealed decide), #26 (origin), #41 (roaster_dedup), #84 (match), #90 (dq #12 and #7) |
| Measured, no floor | #70 (dq #11), #90's #4 part, #68 (no labels) |
