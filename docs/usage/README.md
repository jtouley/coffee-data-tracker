# Usage reports: the private dbt project and the pinned geo database

The analytics design's PRs 3 and 6: the reports built from the anonymous search, filter and click-out events the
collector stores (PR #167). Nothing here is public: the raw events, the geo files and every report live in the
private usage dataset and in `build/usage.duckdb`, which no public job opens.

## How it builds

`coffeedata usage-build [--log-file F] [--catalog DIR] [--alerts-out F]` (`src/coffeedata/usage_build.py`):

1. Reads `config/geo.lock`, finds each locked file in `build/geo/` or copies it there from the usage dataset
   (`$COFFEEDATA_USAGE`, a local folder or `hf://datasets/jtouley/coffee-usage`, read only), and checks its sha256.
   A file that isn't the locked one stops the build before dbt runs.
2. Builds `dbt_usage/`, a dbt project of its own like `dbt_importers/`, into `build/usage.duckdb` (the project
   refuses any other target): models first, then every test, so a failing check never stops a report.
3. Prints counts only. With `--alerts-out`, writes the search-text detector's triage alert for
   `python -m coffeedata.triage prod`: "N searches in week W matched the email pattern", never the text.

`--catalog` names a folder with the served release's `dim_roaster.parquet` and `dim_coffee.parquet`: the ids clicks
are checked against (`usage_click_ids_known`, warn) and the vocabulary a query's words are checked against.
Without it every word counts as unknown, so every query needs 5 days.

## Models

| Layer | Model | Grain |
|---|---|---|
| staging | `stg_usage_events` | event (dedup by `event_id`), every raw column but the IP prefix |
| staging | `stg_usage_ip_prefixes` | event: its prefix only (a view; read only by `int_usage_location`) |
| staging | `stg_geo_ranges`, `stg_geo_cities` | the two locked geo files |
| staging | `stg_usage_catalog` | the served release's roaster and coffee ids and their words |
| intermediate | `int_usage_location` | event: the place every geo row in its prefix's block agrees on |
| intermediate | `int_usage_vocabulary` | word |
| intermediate | `int_usage_events` | event: normalized query, source, location, bot flag |
| intermediate | `int_usage_event_places` | event: the place it may be reported under |
| marts | `mart_usage_page_views_weekly` | week, surface, device |
| marts | `mart_usage_search_terms_weekly` | week, query |
| marts | `mart_usage_filter_combos_weekly` | week, origin, process, state, in_stock, sort |
| marts | `mart_usage_places_weekly` | week, place_type, place (places named in searches) |
| marts | `mart_usage_sources_weekly` | week, source_kind, source, utm_medium, utm_campaign |
| marts | `mart_usage_locations_weekly` | week, country, region, city |
| marts | `mart_usage_city_facets_weekly` | week, country, region, city, facet, facet_value |
| marts | `mart_roaster_clicks_weekly` | week, roaster_id, surface, device, source, country, region |
| marts | `mart_coffee_clicks_weekly` | week, coffee_id |
| marts | `mart_click_context_weekly` | week, roaster_id, context_kind, context_value |
| marts/ops | `ops_usage_build` | locked geo file |

Thresholds (vars in `dbt_usage/dbt_project.yml`): a query, place, filter combination, source, medium, campaign or
click context is shown for a week only if it occurred on 3 distinct UTC days in the 90 days ending on the week's last
day (5 for a query with a word outside the release's vocabulary); a click context counts days with that roaster. A
city is named only with 50,000 people (GeoNames), events on 10 days in the window and 20 in the week, in a region
that passes; otherwise "other, <region>". A city x facet cell needs 10 events, and secondary suppression keeps a
small cell from being worked out by subtraction: cells under 10 merge into one '(other)', and when that is itself
under 10 the facet is held back for that city and week; a place's page views are held back (null) when they or its
other events are under 10. Bots are left out of every mart. The marts carry enforced contracts (every column typed
and described) but are never `access: public`.

**Location is a consensus, never a guess.** The collector keeps only the block (IPv4 /24, IPv6 /40), and a block can
hold DB-IP rows naming several places. `int_usage_location` reads every row inside the block: the city if they all
name one, else the region they all name, else the country, else nothing. DB-IP's "City (Neighbourhood)" names are cut
to the city first, so the neighbourhood is never shown.

## Tests

dbt: enforced contracts on the marts, `unique`/`not_null` on every event model's `event_id`, `accepted_values` on event, surface, device, sort and
source (the collector's values), grain tests on every mart, `usage_click_ids_known` (warn), `usage_events_are_fresh`
(warn, 3 days), `report_rows_meet_threshold`, `cities_meet_floors`, `no_city_with_query_or_roaster`,
`ip_prefix_only_in_location`, `search_text_looks_personal` (error, `store_failures` off), `usage_rows_without_event_id`
(warn), two volume bands (warn), and ten unit tests (`models/unit_tests.yml`). The gates check by meaning:
`no_city_with_query_or_roaster` lists every mart's columns (a renamed or new column fails until reviewed),
`report_rows_meet_threshold` also checks the source beside a roaster's clicks and a city, and `cities_meet_floors`
recomputes each facet's total to catch a hidden cell. The detector counts a phone number only as one is written
(one run of 7-15 digits, groups split by `-` or `.`, a leading `+`, a bracketed area code, or 3-3-4), and a card
only with 13-19 digits that pass the Luhn check, so "gesha 2023 2024" counts as neither.

pytest (`tests/test_usage_dbt.py`), on a synthetic raw dataset and a synthetic geo file: the build and the canary
(203.0.113.0/24, an invented town, `canary@example.com`): the prefix is in no table, the town is only "other,
Colorado", the detector fires with counts and the alert holds no text; each threshold and city rule fails on a
deliberately bad row; two builds (the second reading files and rows in reverse order, in Asia/Tokyo) give the same
rows; the lock and its sha check; byte-for-byte reproducible geo files; the lineage test (no public, served,
exported or contracted model is a usage model, and the main project names nothing from it); the public facts-only
guard (`dbt/tests/public_tables_are_facts_only.sql`) now refuses the usage columns.

The warn tests become errors as their comments say: freshness and the bands after 8 to 12 weeks of real traffic.

## The geo database

**DB-IP "IP to City Lite"**, November 2026 release, CSV (7,777,816 rows). Licence: **CC BY 4.0** ("You are free to
use this IP to City Lite database in your application, provided you give attribution to DB-IP.com", with a link back
on pages that display or use its results). Bundling and redistribution are allowed with attribution; the privacy
page and `docs/ATTRIBUTION.md` need "IP Geolocation by DB-IP" with a link (the beacons PR owns that text).
**GeoNames `cities500`** with `admin1CodesASCII.txt`, downloaded 2026-11-13, **CC BY 4.0**.

`coffeedata geo-update` prepares them (`src/coffeedata/geo.py`): DB-IP becomes a range table (`ip_start`, `ip_end`
as 32 hex digits, IPv4 IPv4-mapped; `country`, `region`, `city`; coordinates dropped; 58 MB); GeoNames becomes the
names of every place of 50,000 people or more (2 MB). Preparing one download twice gives the same bytes, so the
lock's sha256 can be checked by re-preparing. `--old-geo` prints how many stored events would change city.

**Not in git** (58 MB, and a new one monthly): the two prepared files go in the usage dataset under `geo/`, named in
the lock. Proposed fetch: the build copies them from the dataset into `build/geo/` (ignored) and checks the sha256
before every build. **Owner step:** upload `geo/dbip-city-lite-2026-11.parquet` and `geo/geonames-2026-11-13.parquet`
(made in this PR's session; or re-run `coffeedata geo-update` on the same downloads, whose sha256s are in the lock)
to the usage dataset. DB-IP serves only the current month's file, so keep the prepared one.

### What the consensus lookup resolves to, on the pinned file

Every block the file's rows touch, by the level all its rows agree on (shares of blocks, not of traffic; from
`geo.consensus_levels`, which follows the SQL's rule). "Shared" blocks hold two or more rows, or one that covers it
only in part: the only blocks where masking can cost precision. "All" adds the blocks one row covers whole.

| Blocks | City | Region only | Country only | Unknown |
|---|---|---|---|---|
| IPv4 /24, shared (364,284) | 12.2% | 36.5% | 48.2% | 3.2% |
| IPv4 /24, all (16,777,216) | 83.8% | 0.8% | 1.5% | 13.9% (mostly reserved or unallocated) |
| IPv6 /40, shared (346,065) | 10.8% | 30.3% | 54.4% | 4.6% |
| IPv6 /40, all (962,072,674,304) | 99.0% | 0.0% | 0.0% | 1.0% |

The "all" rows are dominated by large single-place allocations, which is also where most addresses sit. Where rows
disagree, the lookup now abstains to the region or country instead of taking the first address's city.

### What first-address lookup would have cost (the first version), measured on the pinned file

| Prefix | Rows | Rows whose masked address finds another place | Addresses that move | Blocks with more than one place |
|---|---|---|---|---|
| IPv4 /24 | 3,598,816 | 336,278 (9.3%) | 1.16% | 333,989 of 16,777,216 (2.0%) |
| IPv6 /40 | 4,178,999 | 2,348,062 (56.2%) | under 0.0001% | 311,833 |
| IPv6 /48 | 4,178,999 | 230,372 (5.5%) | under 0.0001% | 224,388 |

"Addresses that move" weighs each row by its size, so it says little for IPv6, where most of the space is unused.
Row-weighted, **/40 moves a much larger share of DB-IP's IPv6 rows to another place than /48 does (56% against
5.5%)**: on this file, /40 does lose cities. The design's open question said to keep /48 only if /40 loses cities;
the owner has since chosen /40, so this is reported for the owner, not changed.

City names: GeoNames and DB-IP don't always name a place alike. 285,129 of DB-IP's 3,024,195 US rows (9.4%) are
named "City (Neighbourhood)"; cutting the neighbourhood raises the share of US rows matching a place of 50,000 or
more from **53.2% to 62.2%**. The big cities checked match (New York, Los Angeles, Chicago, Denver, Boston, San
Francisco, Honolulu); DB-IP's "Washington D.C." doesn't, so it reports as "other, District of Columbia". Other misses
with many rows (Ashburn, Doraville) are genuinely under 50,000. A miss only ever rolls a city up, never names a small
one.

How to read the city: where the IP block is routed, often the provider's hub or nearest metro. Trust state over city.
