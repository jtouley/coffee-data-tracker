"""What each internal contract's tables and columns mean, and the quality rules on them (raw_contracts.py puts
them into the generated ODCS contracts in contracts/internal/). tests/test_raw_contracts.py fails when a contracted column has no
description here, or a description names a column the lake doesn't have."""

from __future__ import annotations

TABLES: dict[str, str] = {
    "snapshots": "Every product a store listed at one fetch, coffee or not: one row per (fetch batch, product).",
    "bodies": "Product descriptions as text, once per distinct description (body_hash).",
    "fetches": "One row per fetch attempt of a roaster's catalog, ok or not, with what it asked of the store.",
    "run_manifests": "What an ingest set out to fetch, written before its first request: one row per roaster.",
    "job_manifests": "The control plane: one row when a job starts and one when it ends, per connector and week.",
    "enrichments": "Claude's extracted attributes per product description (llm rows; older lakes also hold rules rows).",
    "usage_events": "One anonymous search, filter, click-out or page-view event from registry.coffee or "
    "app.registry.coffee, as the collector accepted it: private, append-only, never published. No identifier of a "
    "person, browser, device, session or page load; no full IP, user agent, cookie, or referrer path or query.",
}

_RUN = "The weekly run (the Monday of its ISO week)."
_BATCH = "The write batch: one per checkpoint of a job, shared by a fetch's row and its snapshots."
_ROASTER = "The roaster's normalized domain."

COLUMNS: dict[str, dict[str, str]] = {
    "snapshots": {
        "run_date": _RUN,
        "batch_id": _BATCH,
        "fetched_at": "When the fetch started (UTC).",
        "roaster_id": _ROASTER,
        "product_id": "The store's product id.",
        "handle": "The store's URL handle for the product.",
        "url": "The product's page on the store.",
        "title": "The product's title as listed.",
        "platform": "The store's platform (shopify, woocommerce, squarespace, square_online, generic).",
        "vendor": "The store's vendor field.",
        "product_type": "The store's product type.",
        "tags": "The store's tags.",
        "image": "The product's first image URL.",
        "body_hash": "Hash of the description; its text is in bodies.",
        "variants": "Each bag size or option: variant id, title, price as the store states it (price_usd: the "
        "name predates currency), grams where known, availability, and currency: the price's currency as the store "
        "states it (ISO 4217), null when the store states none and before raw 1.6.0. dbt publishes USD prices only.",
        "is_coffee": "Whether the crawl's rules called it coffee. Null from an ELT crawl, which doesn't classify.",
        "published_at": "The store's latest publish time (Shopify only).",
        "payload_hash": "Hash of the store's raw product payload: tells a real change from a repeated fetch.",
    },
    "bodies": {
        "body_hash": "Hash of the description text.",
        "body_text": "The description as plain text.",
        "first_seen_run": "The run that first stored it.",
    },
    "fetches": {
        "run_date": _RUN,
        "batch_id": _BATCH,
        "fetched_at": "When the attempt started (UTC).",
        "roaster_id": _ROASTER,
        "adapter": "The catalog adapter used.",
        "status": "ok; error (any failure or block, retried in later passes); unavailable (closed store); "
        "crashed (not fetched: quarantined for the week after it was in flight in two attempts of its crawl that "
        "died).",
        "error": "The failure, when status is not ok; on a partial ok fetch, why it read only part of the catalog.",
        "products": "Products the attempt read.",
        "coffees": "Coffees among them as the crawl counted them; null from an ELT crawl.",
        "pages": "Catalog pages (200 responses other than robots.txt) read.",
        "requests": "HTTP requests made, retries and robots.txt included.",
        "http_statuses": "Responses per HTTP status.",
        "retries": "Attempts after the first for one URL.",
        "rate_limited": "429 responses.",
        "duration_s": "How long the attempt took.",
        "code_version": "The code that made it (git sha).",
        "blocks": "Each refused or blocked response (202/403/429/430/503, or another edge challenge) with its diagnostic headers (sg_captcha from raw 1.4.0) and egress IP.",
        "partial": "True when an ok fetch read only part of the catalog; null (complete) before raw 1.2.0.",
        "crawl_delays": "Each host whose robots.txt asked us a Crawl-delay: the value as written, the spacing used between its requests (the delay, at least 1 s, capped at 60 s; 60 s when it has no readable number) and whether the cap applied; null when none asked, and before raw 1.5.0.",
    },
    "run_manifests": {
        "run_date": _RUN,
        "manifest_id": "One ingest invocation.",
        "started_at": "When it was written, before the first request.",
        "roaster_id": _ROASTER,
        "status": "The roaster's registry status at that moment.",
        "platform": "The roaster's platform.",
        "adapter": "The adapter that will fetch it.",
        "mode": "full, sample, only or retry_failed; only a full manifest is the run's expected set.",
        "code_version": "The code that wrote it.",
    },
    "job_manifests": {
        "run_date": _RUN,
        "job": "discover (connector: the source, osm or overture), probe, crawl, crawl-retry, merge, enrich or fetch-health (the fetch-health check).",
        "connector": "The connector (adapter) the job served, if any.",
        "shard": "The shard, if the job was sharded.",
        "attempt_id": "One execution of the job; its start and end rows share it.",
        "started_at": "When the attempt started.",
        "ended_at": "When it ended (null on the start row).",
        "status": "running (start row), done, deadline or failed. An attempt with only a running row died.",
        "todo": "Roasters it set out to fetch.",
        "ok": "Roasters fetched.",
        "errors": "Roasters that failed.",
        "parked": "Stores parked mid-catalog for crawl-retry.",
        "skipped": "Roasters not started before the deadline.",
        "requests": "HTTP requests made.",
        "http_429": "429 responses.",
        "http_430": "430 responses (Shopify's IP-wide block).",
        "egress_ips": "The egress IPs the job's requests left from.",
        "refusals": "Per egress IP: stores contacted, and those that refused their first catalog request.",
        "code_version": "The code that ran.",
        "detail": "JSON: the job's own summary (arm, egress, rps, slice and salt for the confirmation run; only for a "
        "manual crawl --only; for "
        "fetch-health, its verdict on the week's fetches: systemic fetch failures, and the list of roasters whose "
        "fetch failed, came back empty or came back short, each with its reasons and the gate rules that gave them, "
        "plus the partial and quarantined fetches).",
    },
    "enrichments": {
        "extracted_at": "When it was extracted.",
        "roaster_id": _ROASTER,
        "product_id": "The store's product id.",
        "body_hash": "The description it was extracted from.",
        "method": "llm (Claude), or rules in older lakes.",
        "needs_llm": "The rules left the coffee incompletely described.",
        "origin_country": "Origin country as Claude wrote it (any spelling, or a list); dim_coffee maps it to ISO 3166-1.",
        "origin_region": "Origin region.",
        "producer": "Farm, washing station or producer.",
        "process": "washed, natural, honey, anaerobic or other.",
        "varietals": "Varietals.",
        "elevation_masl_min": "Lowest growing elevation (m).",
        "elevation_masl_max": "Highest growing elevation (m).",
        "roast_level": "light, medium or dark.",
        "tasting_notes": "The roaster's tasting notes.",
        "is_blend": "A blend.",
        "is_decaf": "Decaffeinated.",
    },
    "usage_events": {
        "event_id": "A random version-4 UUID the page makes per event, or null when it sent none (the collector "
        "never makes one up); only for dropping a retried duplicate, never a visitor, session or page-load id.",
        "received_at": "When the collector received it (UTC), truncated to the hour.",
        "event": "page_view, search (results settled), filter_state (filters settled) or click_out (a link to a "
        "roaster's or coffee's store).",
        "surface": "Where it happened: a site page (landing, catalog, privacy, terms, bot) or the app (app_search: the "
        "bag page's catalog search; app_coffee: a coffee page's Shop link).",
        "release": "The catalog build the page was showing (its build id); null in the app.",
        "device": "phone or desktop, as the page judged it from its width.",
        "query": "The search text as typed, capped at 100 characters; not scrubbed. A dbt detector counts any that "
        "look like an email, phone or card number and alerts on the count, never the text.",
        "cities": "Catalog cities the page's parser read in the search text (no '@' or runs of 7+ digits).",
        "states": "Catalog states the page's parser read in the search text, as two-letter codes.",
        "origin": "The Origin filter (a country), or null for all (no '@' or runs of 7+ digits).",
        "process": "The Process filter, or null for all (no '@' or runs of 7+ digits).",
        "state": "The State filter (the roaster's state, a two-letter code), or null for all.",
        "in_stock": "The In stock filter.",
        "sort": "The Sort select: roaster, asc or desc (price per 100 g).",
        "results_n": "Coffees shown for the search and filters.",
        "zero_results": "Whether the search and filters found nothing.",
        "roaster_id": "A click_out's roaster (its normalized domain, the catalog's roaster id), as the page sent it; "
        "checked against the release in dbt.",
        "coffee_id": "A click_out's coffee, if the link was a coffee's: <roaster_id>:<the store's product id>.",
        "rank": "A click_out's position in the result list, from 1; null off a result list.",
        "source": "On a page load's first event only: utm (it had utm_* tags), referrer, direct or internal (one of "
        "our own hosts); null on every later event.",
        "referrer_domain": "The referring site's host name only (never its path or query); null unless source is "
        "referrer, or when the referrer had no usable host name.",
        "utm_source": "The landing URL's utm_source (lower-case, at most 40 of a-z 0-9 . _ -).",
        "utm_medium": "The landing URL's utm_medium, same rules.",
        "utm_campaign": "The landing URL's utm_campaign, same rules.",
        "ip_prefix": "The caller's IP masked at collection, before anything was written: IPv4 /24, IPv6 /40, as a "
        "network (203.0.113.0/24). Personal data: used only to derive country, region and city in dbt "
        "(int_usage_location) and for nothing else. The full IP is never stored or logged.",
        "bot": "A known bot's user agent, no user agent, or navigator.webdriver: excluded in dbt. The user agent "
        "itself is never kept.",
    },
}

# Columns the contract requires to be non-null although the Arrow schema allows null (every lake column is
# nullable, so older files read back): each is held by a quality rule below.
REQUIRED: dict[str, list[str]] = {"fetches": ["run_date", "batch_id", "roaster_id"]}

_SCHEMA_YML = "models/schema.yml"

# Quality rules on each contract's tables, and what enforces each one. engine dbt names a file under dbt/ (a
# singular test) or a schema.yml test on model columns; engine pytest names a test; no engine means the writers
# hold the rule and it is stated as text. enforced_by names what enforces a dbt or text rule (a pytest rule's is its
# implementation). raw_contracts.py emits each as an ODCS quality rule on its tables.
QUALITY: dict[str, list[dict]] = {
    "raw": [
        {"id": "fetch_status_values", "tables": ["fetches"], "dimension": "conformity", "severity": "error",
         "rule": "fetches.status is one of the contract's values",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "stg_fetches", "columns": ["status"],
                                              "test": "accepted_values"},
         "enforced_by": "dbt stg_fetches accepted_values"},
        {"id": "fetch_keys_not_null", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "fetches.roaster_id, run_date and batch_id are not null",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "stg_fetches",
                                              "columns": ["run_date", "batch_id", "roaster_id"], "test": "not_null"},
         "enforced_by": "dbt stg_fetches not_null"},
        {"id": "writer_columns", "tables": ["snapshots", "bodies", "fetches"], "dimension": "conformity",
         "severity": "error", "rule": "a writer writes exactly the contract's columns",
         "engine": "pytest", "implementation": "tests/test_raw_contracts.py::test_ingest_writes_exactly_the_raw_contract"},
        {"id": "run_is_complete", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "every expected roaster of a full run has a fetch",
         "engine": "dbt", "implementation": "tests/run_is_complete.sql", "enforced_by": "dbt run_is_complete"},
        {"id": "run_ok_rate", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "the run's overall ok share is above its floor (unavailable and crashed stores left out)",
         "engine": "dbt", "implementation": "tests/run_ok_rate.sql", "enforced_by": "dbt run_ok_rate"},
        {"id": "adapter_ok_rate", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "each adapter's ok share is above its floor (unavailable and crashed stores left out)",
         "engine": "dbt", "implementation": "tests/adapter_ok_rate.sql", "enforced_by": "dbt adapter_ok_rate"},
        {"id": "suspect_runs_need_review", "tables": ["fetches"], "dimension": "consistency", "severity": "warning",
         "rule": "a roaster's product count doesn't collapse against its baseline unless accepted",
         "engine": "dbt", "implementation": "tests/suspect_runs_need_review.sql",
         "enforced_by": "dbt suspect_runs_need_review (warn)"},
    ],
    "manifest": [
        {"id": "job_start_and_end_rows", "tables": ["job_manifests"], "dimension": "completeness",
         "severity": "error",
         "rule": "a job writes a running row at its start and one end row (done, deadline or failed)",
         "engine": "pytest",
         "implementation": "tests/test_raw_contracts.py::test_control_rows_follow_the_manifest_contract"},
        {"id": "run_manifest_mode_values", "tables": ["run_manifests"], "dimension": "conformity",
         "severity": "error", "rule": "run_manifests.mode is full, sample, only, retry_failed or null",
         "engine": "dbt", "implementation": "tests/run_manifest_mode_values.sql",
         "enforced_by": "dbt run_manifest_mode_values"},
    ],
    "enrichment": [
        {"id": "writer_columns", "tables": ["enrichments"], "dimension": "conformity", "severity": "error",
         "rule": "rows have exactly the contract's columns",
         "engine": "pytest",
         "implementation": "tests/test_raw_contracts.py::test_enrichment_rows_follow_the_enrichment_contract"},
        {"id": "accepted_attribute_values", "tables": ["enrichments"], "dimension": "conformity",
         "severity": "error", "rule": "process and roast_level take the dbt-accepted values",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "dim_coffee",
                                              "columns": ["process", "roast_level"], "test": "accepted_values"},
         "enforced_by": "dbt dim_coffee accepted_values"},
    ],
    "usage": [
        {"id": "writer_columns", "tables": ["usage_events"], "dimension": "conformity", "severity": "error",
         "rule": "the collector writes exactly the contract's columns, and its coded columns only the contract's values",
         "engine": "pytest",
         "implementation": "tests/test_usage_collector.py::test_rows_follow_the_usage_contract"},
        {"id": "ip_prefix_is_masked", "tables": ["usage_events"], "dimension": "conformity", "severity": "error",
         "rule": "ip_prefix is an IPv4 /24 or IPv6 /40 network, never a full address, and no other column holds one",
         "engine": "pytest",
         "implementation": "tests/test_usage_collector.py::test_no_row_can_hold_a_full_address"},
        {"id": "query_capped", "tables": ["usage_events"], "dimension": "conformity", "severity": "error",
         "rule": "query is at most 100 characters",
         "engine": "pytest", "implementation": "tests/test_usage_collector.py::test_query_is_kept_as_typed_and_capped"},
        {"id": "no_identifier", "tables": ["usage_events"], "dimension": "conformity", "severity": "error",
         "rule": "nothing written or logged holds the full IP, a cookie or the user agent",
         "engine": "pytest",
         "implementation": "tests/test_usage_collector.py::test_canary_leaves_no_ip_or_cookie_in_the_lake_or_logs"},
    ],
}  # fmt: skip
