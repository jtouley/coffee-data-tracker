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
        "variants": "Each bag size or option: variant id, title, price (USD), grams where known, availability.",
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
        "status": "ok; error (any failure or block, retried in later passes); unavailable (closed store).",
        "error": "The failure, when status is not ok.",
        "products": "Products the attempt read.",
        "coffees": "Coffees among them as the crawl counted them; null from an ELT crawl.",
        "pages": "Catalog pages (200 responses other than robots.txt) read.",
        "requests": "HTTP requests made, retries and robots.txt included.",
        "http_statuses": "Responses per HTTP status.",
        "retries": "Attempts after the first for one URL.",
        "rate_limited": "429 responses.",
        "duration_s": "How long the attempt took.",
        "code_version": "The code that made it (git sha).",
        "blocks": "Each refused or blocked response (403/429/430/503) with its diagnostic headers and egress IP.",
        "partial": "True when an ok fetch read only part of the catalog; null (complete) before raw 1.2.0.",
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
        "job": "crawl, crawl-retry, merge or enrich.",
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
        "detail": "JSON: the job's own summary (arm, egress, rps, slice and salt for the confirmation run).",
    },
    "enrichments": {
        "extracted_at": "When it was extracted.",
        "roaster_id": _ROASTER,
        "product_id": "The store's product id.",
        "body_hash": "The description it was extracted from.",
        "method": "llm (Claude), or rules in older lakes.",
        "needs_llm": "The rules left the coffee incompletely described.",
        "origin_country": "Origin country.",
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
}

# Columns the contract requires to be non-null although the Arrow schema allows null (every lake column is
# nullable, so older files read back): each is held by a quality rule below.
REQUIRED: dict[str, list[str]] = {"fetches": ["run_date", "batch_id", "roaster_id"]}

_SCHEMA_YML = "models/schema.yml"

# Quality rules on each contract's tables, and what enforces each one. engine dbt names a file under dbt/ (a
# singular test) or a schema.yml test on model columns; engine pytest names a test; no engine means the writers
# hold the rule and it is stated as text. raw_contracts.py emits each as an ODCS quality rule on its tables.
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
         "engine": "pytest", "implementation": "tests/test_raw_contracts.py::test_ingest_writes_exactly_the_raw_contract",
         "enforced_by": "tests/test_raw_contracts.py::test_ingest_writes_exactly_the_raw_contract"},
        {"id": "run_is_complete", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "every expected roaster of a full run has a fetch",
         "engine": "dbt", "implementation": "tests/run_is_complete.sql", "enforced_by": "dbt run_is_complete"},
        {"id": "run_ok_rate", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "the run's overall ok share is above its floor (unavailable stores left out)",
         "engine": "dbt", "implementation": "tests/run_ok_rate.sql", "enforced_by": "dbt run_ok_rate"},
        {"id": "adapter_ok_rate", "tables": ["fetches"], "dimension": "completeness", "severity": "error",
         "rule": "each adapter's ok share is above its floor (unavailable stores left out)",
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
         "implementation": "tests/test_raw_contracts.py::test_control_rows_follow_the_manifest_contract",
         "enforced_by": "tests/test_raw_contracts.py::test_control_rows_follow_the_manifest_contract"},
        {"id": "run_manifest_mode_values", "tables": ["run_manifests"], "dimension": "conformity",
         "severity": "error", "rule": "run_manifests.mode is full, sample, only, retry_failed or null",
         "enforced_by": "the writers (ingest.select_states, connectors.crawl, crawl-retry)"},
    ],
    "enrichment": [
        {"id": "writer_columns", "tables": ["enrichments"], "dimension": "conformity", "severity": "error",
         "rule": "rows have exactly the contract's columns",
         "engine": "pytest",
         "implementation": "tests/test_raw_contracts.py::test_enrichment_rows_follow_the_enrichment_contract",
         "enforced_by": "tests/test_raw_contracts.py::test_enrichment_rows_follow_the_enrichment_contract"},
        {"id": "accepted_attribute_values", "tables": ["enrichments"], "dimension": "conformity",
         "severity": "error", "rule": "process and roast_level take the dbt-accepted values",
         "engine": "dbt", "implementation": {"file": _SCHEMA_YML, "model": "dim_coffee",
                                              "columns": ["process", "roast_level"], "test": "accepted_values"},
         "enforced_by": "dbt dim_coffee accepted_values"},
    ],
}  # fmt: skip
