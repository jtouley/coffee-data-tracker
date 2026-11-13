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
    "run_completeness": "Ops: one row per roaster the build's run should have tried, and how its fetch went "
    "(dbt run_completeness, as the build saw it).",
    "release_changes": "Ops: every row the build's public tables add, remove or change against the release served "
    "before it, with the reason (dbt release_changes); empty when nothing was served.",
    "product_decisions": "Ops: every product a store lists, coffee or not, as of its latest record, with the "
    "build's coffee decision, what decided it and why (dbt product_decisions).",
    "mart_flags": "Ops: every flag a rule raised in the build, one row per rule and product, with its polarity, "
    "evidence and the product's summary: ready to remove, and its evidence group (dbt mart_flags).",
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
        "job": "discover (connector: the source, osm or overture), probe, crawl, crawl-retry, merge, enrich, "
        "fetch-health (the fetch-health check), classify (Claude's verdicts) or build (the dbt build, its release and "
        "serve).",
        "connector": "The connector (adapter) the job served, if any.",
        "shard": "The shard, if the job was sharded.",
        "attempt_id": "One execution of the job; its start and end rows share it.",
        "started_at": "When the attempt started.",
        "ended_at": "When it ended (null on the start row).",
        "status": "running (start row), done, deadline or failed. An attempt with only a running row died. A "
        "build is done when it passed and was served, failed when its checks failed or it crashed (detail.passed, "
        "detail.error).",
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
        "plus the partial and quarantined fetches; for classify, its model and what it collected and submitted; "
        "for build, its build_id and whether it passed).",
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

_OPS_RUN = "The run the build judged (as_of_run, else the latest run)."
_OPS_BUILD = "The build that wrote it (builds.build_id, the folder's build=<id>): every row names its build."
COLUMNS |= {
    "run_completeness": {
        "build_id": _OPS_BUILD,
        "run_date": _OPS_RUN,
        "roaster_id": "A roaster the run should have tried.",
        "attempted": "The run has a fetch for it.",
        "ok": "An attempt in the run was ok.",
        "expected_from": "Where the expected set came from: manifest or reconstructed.",
        "adapter": "The adapter that counts for it in adapter_ok_rate.",
        "has_catalog": "It counts in the ok rates: not known to have no catalog, not unavailable, not quarantined.",
        "catalog_ok": "The run fetched it and got products.",
        "unavailable": "Found closed in this run (fetch status unavailable, no ok attempt).",
        "quarantined": "Quarantined in this run (fetch status crashed, no ok attempt): not fetched.",
        "partial": "This run read only part of its catalog.",
        "partial_reason": "Why it is partial; null otherwise.",
        "challenge": "The edge whose challenge it failed behind (siteground or cloudflare); null otherwise.",
    },
    "release_changes": {
        "build_id": _OPS_BUILD,
        "table_name": "The public table, or 'release' when the served release couldn't be read.",
        "item": "The row's key in that table; for a schema row the missing column or table.",
        "change": "added, removed, changed, schema or not_reconciled.",
        "reason": "Why, from the two releases and the build; unexplained when nothing shows why.",
    },
    "product_decisions": {
        "build_id": _OPS_BUILD,
        "coffee_id": "<roaster domain>:<store product id> (dim_coffee's key, for the ones that are coffee).",
        "roaster_id": _ROASTER,
        "product_id": "The store's product id.",
        "title": "The title as the store sent it (latest record).",
        "product_type": "The store's product type (latest record).",
        "url": "The product's page on the store, as sent.",
        "first_seen": "The first run with a record of it.",
        "last_seen": "The run of its latest record, which the decision is about.",
        "is_listed": "Its latest record is from the roaster's current run.",
        "input_hash": "The product version the decision is about.",
        "is_coffee": "The build counts it as coffee (dim_coffee holds it).",
        "classified_by": "claude (a stored verdict decided) or rules.",
        "verdict_model": "The verdict's model when classified_by is claude, else null.",
        "verdict_label": "Claude's stored label for the version (coffee, not_coffee or unsure), whether or not it "
        "decided; null without one.",
        "reason": "Why: verdict:<label> when Claude's verdict decided, else the rule that did: excluded:<rule>, sauce, "
        "sweets, drinks, coffee_words, country, demonym, coffee_type or signals:<n>; decision:<action> when the "
        "owner's decision overrides them.",
    },
    "mart_flags": {
        "build_id": _OPS_BUILD,
        "rule": "The rule that raised the flag (coffeedata.rules.RULE_TYPES).",
        "family": "The rule's family (classifier, catalog or listing): readiness counts distinct families.",
        "polarity": "supports (a removal), doubts (a removal) or restore, from the build's rule settings.",
        "direction": "The rule's direction (verdict_flip: added or removed); empty for a rule with one.",
        "coffee_id": "The product flagged.",
        "roaster_id": _ROASTER,
        "input_hash": "The product version its latest record is (the owner can decide by it).",
        "title": "The title as the store sent it (latest record).",
        "product_type": "The store's product type (latest record).",
        "evidence": "What the rule saw, as JSON (shown on the admin's Case page).",
        "evidence_key": "The evidence without the product's identity.",
        "evidence_hash": "A hash of the store and every flag's evidence_key on the product: a store's products with "
        "equal evidence group.",
        "decision": "The owner's decision the build applied (include or exclude), else null.",
        "classifier_is_coffee": "The classifier's answer for the latest version, before any decision.",
        "was_public": "In the served release's public_coffee.",
        "ready_flags": "Its removal is ready as far as the build can tell (supports from enough families, no doubt, "
        "no coverage problem); the admin adds the live check.",
        "coverage_problem": "This run failed to fetch the product's store although it has a catalog.",
        "supports_families": "Distinct families of its supports flags.",
        "doubts": "Its doubts flags.",
        "restores": "Its restore flags.",
        "public_rows": "Public rows a removal touches: its public_coffee row and its public_price_event rows.",
        "verdict_label": "Claude's stored label for the latest version, if any.",
        "classified_by": "claude (a stored verdict decided) or rules.",
        "reason": "Why the classifier answered as it did (stg_products.reason).",
        "url": "The product's page on the store, as sent.",
        "tags": "The store's tags (latest record).",
        "bag_sizes_g": "Each variant's weight in grams, as sent.",
        "prices_usd": "Each variant's price in USD (null when the store states another currency).",
        "description": "The description's text, first 2,000 characters.",
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
    "ops": [
        {"id": "ops_columns", "tables": ["run_completeness", "release_changes", "product_decisions", "mart_flags"],
         "dimension": "conformity", "severity": "error",
         "rule": "each ops file has exactly the contract's columns and types, and none is a release table",
         "engine": "pytest",
         "implementation": "tests/test_raw_contracts.py::test_ops_tables_follow_the_ops_contract"},
        {"id": "one_row_per_roaster", "tables": ["run_completeness"], "dimension": "uniqueness",
         "severity": "error", "rule": "run_completeness.roaster_id is unique and not null",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "run_completeness",
                                              "columns": ["roaster_id"], "test": "unique"},
         "enforced_by": "dbt run_completeness unique, not_null"},
        {"id": "change_values", "tables": ["release_changes"], "dimension": "conformity", "severity": "warning",
         "rule": "release_changes.change is added, removed, changed, schema or not_reconciled",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "release_changes",
                                              "columns": ["change"], "test": "accepted_values"},
         "enforced_by": "dbt release_changes accepted_values (report only: never gates a release)"},
        {"id": "one_row_per_product", "tables": ["product_decisions"], "dimension": "uniqueness",
         "severity": "warning", "rule": "product_decisions.coffee_id is unique and not null, and every row has a "
         "decision and a reason",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "product_decisions",
                                              "columns": ["coffee_id"], "test": "unique"},
         "enforced_by": "dbt product_decisions unique, not_null (report only: never gates a release)"},
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
