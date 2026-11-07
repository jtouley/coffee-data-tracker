"""The data lake: immutable, partitioned Parquet files on local disk or a Hugging Face dataset.

Every pipeline step *adds* files and never rewrites another run's files:

    snapshots/run_date=YYYY-MM-DD/batch=<id>.parquet      every product seen, per fetch batch
    bodies/run_date=YYYY-MM-DD/batch=<id>.parquet         product description text, once per new body_hash
    fetches/run_date=YYYY-MM-DD/batch=<id>.parquet        one row per roaster fetch attempt (ok / error)
    probes/run_date=YYYY-MM-DD/batch=<id>.parquet         storefront platform detection results
    registry/source=<s>/<key>.parquet                     roasters added by discovery or by hand
    enrichments/method=<m>/run_date=.../batch=<id>.parquet extracted attributes, keyed by body_hash
    llm_batches/state=<submitted|collected>/<id>.parquet  Claude Message Batches bookkeeping
    shots/<shot_id>__<time>.parquet                       one file per logged shot (or correction)
    equipment/<equipment_id>__<time>.parquet              one file per grinder, machine or basket change
    bags/<bag_id>__<time>.parquet                         one file per bag opened (or changed)
    run_manifests/run_date=YYYY-MM-DD/batch=<id>.parquet  the roasters an ingest set out to fetch, and their status
    serving_log/serve_id=<id>.parquet                     one row per serve, rollback or re-export of a release
    releases/run_date=.../build=<id>/<table>.parquet       every build's release (quality.RELEASE_TABLES)
    releases/run_date=.../build=<id>/ops/<table>.parquet   its ops tables (quality.OPS_TABLES): never public

Bag photos go to their own private dataset (`Lake.photos()`), one photo per file:

    photos/<photo_id>.parquet                             the image, in Hugging Face's Image layout

The app's accounts go to a third private dataset (`Lake.users()`), never read by dbt:

    users/<user_id>__<time>.parquet                       one file per sign-up or account change
    invites/<invite_id>__<time>.parquet                   one file per invite or reset code (or its use)

Current state ("what's listed now", "is this roaster reachable", price history) is always *derived* from these
files, in dbt and in `state.py`, so re-running a step is safe: duplicates collapse on their natural keys.

For a remote lake (``hf://datasets/<owner>/<name>``) files are staged locally and published in one commit per
checkpoint (each ingest chunk and each weekly step).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

DEFAULT_URL = "lake"

VARIANT = pa.struct(
    [
        ("variant_id", pa.string()),
        ("title", pa.string()),
        ("price_usd", pa.float64()),
        ("grams", pa.int32()),
        ("available", pa.bool_()),
        # The price's currency as the store states it (ISO 4217); added in raw 1.6.0, null in older files.
        ("currency", pa.string()),
    ]
)

# One HTTP status a fetch saw, and how many responses had it.
# fetches.status: fetched; failed (any error or block, retried in later passes); the store is closed, deactivated or
# password-protected (http.Unavailable: not retried this run, and left out of the run's ok rates); or quarantined
# for the week, not fetched, after it was in flight in two attempts of its crawl that died (crashguard: not retried
# this run, and left out of the ok rates).
FETCH_STATUSES = ("ok", "error", "unavailable", "crashed")
HTTP_STATUS = pa.struct([("status", pa.int32()), ("responses", pa.int32())])
# A host whose robots.txt asked us a Crawl-delay: the value as written, the spacing its requests got, and whether
# the 60 s cap applied (http.RequestStats.crawl_delays).
CRAWL_DELAY = pa.struct(
    [("host", pa.string()), ("asked", pa.string()), ("applied_s", pa.float64()), ("capped", pa.bool_())]
)
# One refused or blocked response (202/403/429/430/503, or another edge challenge), with the headers that tell a rate
# limit from an edge challenge (Cloudflare's cf-mitigated, SiteGround's sg-captcha) and that Shopify Support asks
# for, and the egress IP it left from (http.block_record). sg_captcha: added in raw 1.4.0 (null in older files).
BLOCK = pa.struct(
    [
        ("at", pa.timestamp("s", tz="UTC")),
        ("status", pa.int32()),
        ("host", pa.string()),
        ("path", pa.string()),
        ("server", pa.string()),
        ("cf_mitigated", pa.string()),
        ("content_type", pa.string()),
        ("retry_after", pa.string()),
        ("cf_ray", pa.string()),
        ("x_request_id", pa.string()),
        ("egress_ip", pa.string()),
        ("sg_captcha", pa.string()),
    ]
)

# Hugging Face's Image feature: the dataset viewer shows a thumbnail for a struct of bytes and path.
IMAGE = pa.struct([("bytes", pa.binary()), ("path", pa.string())])


def hf_features(schema: pa.Schema) -> str:
    """The `huggingface` Parquet metadata that tells the dataset viewer which column is an image."""
    feats = {
        f.name: {"_type": "Image"} if f.type == IMAGE else {"dtype": str(f.type), "_type": "Value"}
        for f in schema
    }
    return json.dumps({"info": {"features": feats}})


SCHEMAS: dict[str, pa.Schema] = {
    "snapshots": pa.schema(
        [
            ("run_date", pa.date32()),
            ("batch_id", pa.string()),
            ("fetched_at", pa.timestamp("s", tz="UTC")),
            ("roaster_id", pa.string()),
            ("product_id", pa.string()),
            ("handle", pa.string()),
            ("url", pa.string()),
            ("title", pa.string()),
            ("platform", pa.string()),
            ("vendor", pa.string()),
            ("product_type", pa.string()),
            ("tags", pa.list_(pa.string())),
            ("image", pa.string()),
            ("body_hash", pa.string()),
            ("variants", pa.list_(VARIANT)),
            ("is_coffee", pa.bool_()),
            ("published_at", pa.timestamp("s", tz="UTC")),
            # Added later; older files read back as null. A hash of the store's raw product payload, so a
            # product that really changed, and a fetch that repeated an earlier one, can be told apart.
            ("payload_hash", pa.string()),
        ]
    ),
    "bodies": pa.schema(
        [("body_hash", pa.string()), ("body_text", pa.string()), ("first_seen_run", pa.date32())]
    ),
    "fetches": pa.schema(
        [
            ("run_date", pa.date32()),
            ("batch_id", pa.string()),
            ("fetched_at", pa.timestamp("s", tz="UTC")),
            ("roaster_id", pa.string()),
            ("adapter", pa.string()),
            ("status", pa.string()),
            ("error", pa.string()),
            ("products", pa.int32()),
            ("coffees", pa.int32()),
            # Fetch diagnostics, added later (older files read back as null): what the fetch asked of the
            # store (http.RequestStats), how long it took, and the code that did it.
            ("pages", pa.int32()),
            ("requests", pa.int32()),
            ("http_statuses", pa.list_(HTTP_STATUS)),
            ("retries", pa.int32()),
            ("rate_limited", pa.int32()),
            ("duration_s", pa.float64()),
            ("code_version", pa.string()),
            # Added with raw contract 1.1.0 (older files read back as null): each refused or blocked response.
            ("blocks", pa.list_(BLOCK)),
            # Added with raw contract 1.2.0 (older files read back as null): true when the fetch read only part of
            # the catalog (a secondary crawl stopped mid-way). Its products are listed; the products it didn't
            # read keep their listing (dbt stg_runs.is_current), instead of reading as delisted.
            ("partial", pa.bool_()),
            # Added with raw contract 1.5.0 (older files read back as null): each host whose robots.txt asked a
            # Crawl-delay, the value as written, the spacing used (capped at http.MAX_CRAWL_DELAY), and whether the
            # cap applied.
            ("crawl_delays", pa.list_(CRAWL_DELAY)),
        ]
    ),
    "probes": pa.schema(
        [
            ("run_date", pa.date32()),
            ("batch_id", pa.string()),
            ("probed_at", pa.timestamp("s", tz="UTC")),
            ("roaster_id", pa.string()),
            ("status", pa.string()),
            ("http_status", pa.int32()),
            ("final_host", pa.string()),
            ("platform", pa.string()),
        ]
    ),
    "registry": pa.schema(
        [
            ("added_at", pa.timestamp("s", tz="UTC")),
            ("source", pa.string()),
            ("roaster_id", pa.string()),
            ("name", pa.string()),
            ("city", pa.string()),
            ("state", pa.string()),
            ("locations", pa.int32()),
            ("confidence", pa.float64()),
            ("tagged_roastery", pa.bool_()),
            ("roast_in_name", pa.bool_()),
            ("opted_out", pa.bool_()),
        ]
    ),
    "enrichments": pa.schema(
        [
            ("extracted_at", pa.timestamp("s", tz="UTC")),
            ("roaster_id", pa.string()),
            ("product_id", pa.string()),
            ("body_hash", pa.string()),
            ("method", pa.string()),
            ("needs_llm", pa.bool_()),
            ("origin_country", pa.string()),
            ("origin_region", pa.string()),
            ("producer", pa.string()),
            ("process", pa.string()),
            ("varietals", pa.list_(pa.string())),
            ("elevation_masl_min", pa.int32()),
            ("elevation_masl_max", pa.int32()),
            ("roast_level", pa.string()),
            ("tasting_notes", pa.list_(pa.string())),
            ("is_blend", pa.bool_()),
            ("is_decaf", pa.bool_()),
        ]
    ),
    "llm_batches": pa.schema(
        [
            ("batch_id", pa.string()),
            ("state", pa.string()),
            ("at", pa.timestamp("s", tz="UTC")),
            ("custom_id", pa.string()),
            ("roaster_id", pa.string()),
            ("product_id", pa.string()),
            ("body_hash", pa.string()),
        ]
    ),
    # Claude's verdict on whether a product version (stg_snapshots.input_hash) is a coffee, per model and
    # definition version. Stored like any other fetched fact, so builds stay reproducible.
    "verdicts": pa.schema(
        [
            ("decided_at", pa.timestamp("s", tz="UTC")),
            ("input_hash", pa.string()),
            ("model", pa.string()),
            ("definition", pa.string()),
            ("label", pa.string()),
        ]
    ),
    # A person's call on one roaster-week's fetch: accept a run the quality checks flagged, reject one, or pin
    # which batch counts (use_batch). The latest override for a roaster-week wins; nothing is ever edited.
    "run_overrides": pa.schema(
        [
            ("added_at", pa.timestamp("s", tz="UTC")),
            ("roaster_id", pa.string()),
            ("run_date", pa.date32()),
            ("action", pa.string()),
            ("batch_id", pa.string()),
            ("reason", pa.string()),
        ]
    ),
    # One file per dbt build: a row per dbt node (its manifest checksum and test result, so two builds' code
    # and test outcomes can be compared), a row per catalog metric (the baseline the next build's volume check
    # reads), a row per release table (kind table_hash: its row count in rows, its content hash in checksum and the
    # hash method in payload, quality.table_hash), a row per ops table written (kind ops_table: its rows), a row per
    # release change count (kind release_change: rows of a
    # public table added, removed or changed against the served release, per reason; the release's notes), and the
    # build's manifest.json and run_results.json as text, so `dbt --state` can use them later.
    "builds": pa.schema(
        [
            ("build_id", pa.string()),
            ("built_at", pa.timestamp("s", tz="UTC")),
            ("run_date", pa.date32()),
            ("git_sha", pa.string()),
            ("invocation_id", pa.string()),
            ("kind", pa.string()),
            ("unique_id", pa.string()),
            ("resource_type", pa.string()),
            ("checksum", pa.string()),
            ("status", pa.string()),
            ("failures", pa.int64()),
            ("rows", pa.int64()),
            ("execution_s", pa.float64()),
            ("value", pa.float64()),
            ("payload", pa.string()),
            # The build's environment, added later (older files read back as null): the CI run, dbt's and the
            # key packages' versions (a JSON object) and how long dbt took.
            ("ci_run_id", pa.string()),
            ("ci_run_url", pa.string()),
            ("dbt_version", pa.string()),
            ("package_versions", pa.string()),
            ("duration_s", pa.float64()),
        ]
    ),
    # What an ingest set out to fetch, written before its first request: one row per roaster it intends to
    # fetch, with that roaster's status at that moment. run_completeness reads it as the run's expected set.
    # The control plane (design B): one row when a job starts and one when it ends: crawl, crawl-retry, merge or
    # enrich, per connector and week. Services read it instead of calling each other; the weekly merge rolls a
    # week's rows into one file. Never read by dbt.
    "job_manifests": pa.schema(
        [
            ("run_date", pa.date32()),
            ("job", pa.string()),
            ("connector", pa.string()),
            ("shard", pa.string()),
            ("attempt_id", pa.string()),
            ("started_at", pa.timestamp("s", tz="UTC")),
            ("ended_at", pa.timestamp("s", tz="UTC")),
            ("status", pa.string()),  # running, done, deadline, failed
            ("todo", pa.int32()),
            ("ok", pa.int32()),
            ("errors", pa.int32()),
            ("parked", pa.int32()),
            ("skipped", pa.int32()),
            ("requests", pa.int32()),
            ("http_429", pa.int32()),
            ("http_430", pa.int32()),
            ("egress_ips", pa.list_(pa.string())),
            # per egress IP: stores contacted, and those that refused (429/430) their first catalog request
            (
                "refusals",
                pa.list_(pa.struct([("ip", pa.string()), ("stores", pa.int32()), ("refused", pa.int32())])),
            ),
            ("code_version", pa.string()),
            ("detail", pa.string()),  # JSON: the job's own summary
        ]
    ),
    "run_manifests": pa.schema(
        [
            ("run_date", pa.date32()),
            ("manifest_id", pa.string()),
            ("started_at", pa.timestamp("s", tz="UTC")),
            ("roaster_id", pa.string()),
            ("status", pa.string()),
            ("platform", pa.string()),
            ("adapter", pa.string()),
            ("mode", pa.string()),  # full, sample, only or retry_failed
            ("code_version", pa.string()),
        ]
    ),
    # One row per serve, rollback and re-export: which release customers got, when, why, from whom, and what it
    # replaced. marts/release.parquet holds only the current one.
    "serving_log": pa.schema(
        [
            ("served_at", pa.timestamp("s", tz="UTC")),
            ("action", pa.string()),  # serve, rollback or reexport (the served release again)
            ("build_id", pa.string()),
            ("run_date", pa.date32()),
            ("reason", pa.string()),
            ("actor", pa.string()),
            ("previous_build_id", pa.string()),
            ("ci_run_id", pa.string()),
            ("ci_run_url", pa.string()),
        ]
    ),
    # The ops tables: what each build saw, written beside its release (releases/.../build=<id>/ops/<table>.parquet,
    # quality.OPS_TABLES) for the owner's command center. Copied from the build's dbt models with the build's id
    # first (every row says which build it is, however the files are read), cast to these schemas; never served,
    # exported, published as public tables or read by dbt. Not Lake tables: no GLOBS entry or schema file, so
    # Lake.scan never reads them; quality.ops_files(lake, build_id) finds one build's (OPS_GLOBS: all builds').
    # contracts/internal/ops.odcs.yaml is their contract.
    "run_completeness": pa.schema(
        [
            ("build_id", pa.string()),
            ("run_date", pa.date32()),
            ("roaster_id", pa.string()),
            ("attempted", pa.bool_()),
            ("ok", pa.bool_()),
            ("expected_from", pa.string()),
            ("adapter", pa.string()),
            ("has_catalog", pa.bool_()),
            ("catalog_ok", pa.bool_()),
            ("unavailable", pa.bool_()),
            ("quarantined", pa.bool_()),
            ("partial", pa.bool_()),
            ("partial_reason", pa.string()),
            ("challenge", pa.string()),
        ]
    ),
    "release_changes": pa.schema(
        [
            ("build_id", pa.string()),
            ("table_name", pa.string()),
            ("item", pa.string()),
            ("change", pa.string()),
            ("reason", pa.string()),
        ]
    ),
    "product_decisions": pa.schema(
        [
            ("build_id", pa.string()),
            ("coffee_id", pa.string()),
            ("roaster_id", pa.string()),
            ("product_id", pa.string()),
            ("title", pa.string()),
            ("product_type", pa.string()),
            ("url", pa.string()),
            ("first_seen", pa.date32()),
            ("last_seen", pa.date32()),
            ("is_listed", pa.bool_()),
            ("input_hash", pa.string()),
            ("is_coffee", pa.bool_()),
            ("classified_by", pa.string()),
            ("verdict_model", pa.string()),
            ("verdict_label", pa.string()),
            ("reason", pa.string()),
        ]
    ),
    "verdict_batches": pa.schema(
        [
            ("batch_id", pa.string()),
            ("state", pa.string()),
            ("at", pa.timestamp("s", tz="UTC")),
            ("custom_id", pa.string()),
            ("model", pa.string()),
            ("definition", pa.string()),
        ]
    ),
    "shots": pa.schema(
        [
            ("shot_id", pa.string()),
            ("logged_at", pa.timestamp("s", tz="UTC")),
            ("deleted", pa.bool_()),
            ("brewed_at", pa.timestamp("s", tz="UTC")),
            ("coffee_id", pa.string()),
            ("coffee_freetext", pa.string()),
            ("roast_date", pa.date32()),
            ("grinder_setting", pa.float64()),
            ("dose_g", pa.float64()),
            ("yield_g", pa.float64()),
            ("time_s", pa.float64()),
            ("water_temp_c", pa.float64()),
            ("brew_method", pa.string()),
            ("rating", pa.int32()),
            ("taste", pa.string()),
            ("is_milk_drink", pa.bool_()),
            ("notes", pa.string()),
            # Added with the app. Old shot files don't have these; they read back as null.
            ("user_id", pa.string()),
            ("grinder_id", pa.string()),
            ("machine_id", pa.string()),
            ("basket_id", pa.string()),
            ("bag_id", pa.string()),
            ("source", pa.string()),
        ]
    ),
    "equipment": pa.schema(
        [
            ("equipment_id", pa.string()),
            ("user_id", pa.string()),
            ("logged_at", pa.timestamp("s", tz="UTC")),
            ("kind", pa.string()),
            ("name", pa.string()),
            ("model", pa.string()),
            ("scale_min", pa.float64()),
            ("scale_max", pa.float64()),
            ("scale_step", pa.float64()),
            ("finer_is_lower", pa.bool_()),
            ("basket_dose_g", pa.float64()),
            ("is_default", pa.bool_()),
            ("retired", pa.bool_()),
            ("notes", pa.string()),
        ]
    ),
    "bags": pa.schema(
        [
            ("bag_id", pa.string()),
            ("user_id", pa.string()),
            ("logged_at", pa.timestamp("s", tz="UTC")),
            ("coffee_id", pa.string()),
            ("coffee_freetext", pa.string()),
            ("roast_date", pa.date32()),
            ("opened_on", pa.date32()),
            ("finished_on", pa.date32()),
            ("weight_g", pa.int32()),
            ("photo_id", pa.string()),
            ("read_json", pa.string()),
            ("match_score", pa.int32()),
            ("deleted", pa.bool_()),
        ]
    ),
    "photos": pa.schema(
        [
            ("photo_id", pa.string()),
            ("user_id", pa.string()),
            ("taken_at", pa.timestamp("s", tz="UTC")),
            ("image", IMAGE),
            ("media_type", pa.string()),
            ("width", pa.int32()),
            ("height", pa.int32()),
            ("bytes", pa.int32()),
            ("sha256", pa.string()),
        ]
    ),
}
SCHEMAS["photos"] = SCHEMAS["photos"].with_metadata({"huggingface": hf_features(SCHEMAS["photos"])})
# The app's accounts, in their own private dataset: nothing that reads the lake can see a password hash.
SCHEMAS["users"] = pa.schema(
    [
        ("user_id", pa.string()),
        ("logged_at", pa.timestamp("s", tz="UTC")),
        ("username", pa.string()),
        ("display_name", pa.string()),
        ("password_hash", pa.string()),  # Argon2id PHC string, salt included
        ("role", pa.string()),
        ("session_epoch", pa.int32()),
        ("invite_id", pa.string()),
        ("disabled", pa.bool_()),
    ]
)
SCHEMAS["invites"] = pa.schema(
    [
        ("invite_id", pa.string()),
        ("logged_at", pa.timestamp("s", tz="UTC")),
        ("code_sha256", pa.string()),
        ("kind", pa.string()),  # join or reset
        ("for_user_id", pa.string()),
        ("created_by", pa.string()),
        ("expires_at", pa.timestamp("s", tz="UTC")),
        ("used_by", pa.string()),
        ("used_at", pa.timestamp("s", tz="UTC")),
        ("revoked", pa.bool_()),
        ("for_label", pa.string()),  # the owner's note on a join link ("For"), up to 60 characters
        ("created_at", pa.timestamp("s", tz="UTC")),  # null in files from before it was kept
        ("revoked_at", pa.timestamp("s", tz="UTC")),  # null in files from before it was kept
    ]
)
# Which Terms and Privacy Policy each account accepted, and when: one file per acceptance, latest wins.
SCHEMAS["consents"] = pa.schema(
    [
        ("user_id", pa.string()),
        ("logged_at", pa.timestamp("s", tz="UTC")),
        ("terms_version", pa.string()),  # the Terms' "Last updated" date (site.LEGAL_UPDATED)
        ("privacy_version", pa.string()),  # the Privacy Policy's
        ("terms_at", pa.timestamp("s", tz="UTC")),
        ("source", pa.string()),  # signup or reaccept
    ]
)

# Tables that live in the photo dataset or the users dataset rather than the main lake.
PHOTO_TABLES = ("photos",)
USER_TABLES = ("users", "invites", "consents")

# Glob (relative to the lake root) that matches every file of a table, including its empty schema file.
GLOBS = {
    "snapshots": "snapshots/*/*.parquet",
    "bodies": "bodies/*/*.parquet",
    "fetches": "fetches/*/*.parquet",
    "probes": "probes/*/*.parquet",
    "registry": "registry/*/*.parquet",
    "enrichments": "enrichments/*/*/*.parquet",
    "llm_batches": "llm_batches/*/*.parquet",
    "verdicts": "verdicts/*/*.parquet",
    "verdict_batches": "verdict_batches/*/*.parquet",
    "run_overrides": "run_overrides/*.parquet",
    "builds": "builds/*/*.parquet",
    "run_manifests": "run_manifests/*/*.parquet",
    "job_manifests": "job_manifests/*/*.parquet",
    "serving_log": "serving_log/*.parquet",
    "shots": "shots/*.parquet",
    "equipment": "equipment/*.parquet",
    "bags": "bags/*.parquet",
    "photos": "photos/*.parquet",
    "users": "users/*.parquet",
    "invites": "invites/*.parquet",
    "consents": "consents/*.parquet",
}
# The ops tables' files across every build (see SCHEMAS): for a reader that globs them itself, with
# hive_partitioning off (build_id is a column). Not in GLOBS: Lake.scan doesn't read them.
OPS_GLOBS = {
    t: f"releases/*/*/ops/{t}.parquet" for t in ("run_completeness", "release_changes", "product_decisions")
}
# Where each table's zero-row schema file lives, so a glob over an empty lake still resolves.
SCHEMA_FILES = {
    "snapshots": "snapshots/run_date=0000-00-00/batch=schema.parquet",
    "bodies": "bodies/run_date=0000-00-00/batch=schema.parquet",
    "fetches": "fetches/run_date=0000-00-00/batch=schema.parquet",
    "probes": "probes/run_date=0000-00-00/batch=schema.parquet",
    "registry": "registry/source=schema/schema.parquet",
    "enrichments": "enrichments/method=schema/run_date=0000-00-00/batch=schema.parquet",
    "llm_batches": "llm_batches/state=schema/schema.parquet",
    "verdicts": "verdicts/run_date=0000-00-00/batch=schema.parquet",
    "verdict_batches": "verdict_batches/state=schema/schema.parquet",
    "run_overrides": "run_overrides/override_id=schema.parquet",
    "builds": "builds/run_date=0000-00-00/build=schema.parquet",
    "run_manifests": "run_manifests/run_date=0000-00-00/batch=schema.parquet",
    "job_manifests": "job_manifests/run_date=0000-00-00/attempt=schema.parquet",
    "serving_log": "serving_log/serve_id=schema.parquet",
    # v2 adds the app's columns. Old lakes keep their v1 file; this one is added beside it, so a glob over
    # only old shot files still has every column.
    "shots": "shots/shot_id=schema-v2.parquet",
    "equipment": "equipment/equipment_id=schema.parquet",
    "bags": "bags/bag_id=schema.parquet",
    "photos": "photos/photo_id=schema.parquet",
    "users": "users/user_id=schema.parquet",
    "invites": "invites/invite_id=schema.parquet",
    "consents": "consents/user_id=schema.parquet",
}
LAKE_TABLES = tuple(t for t in SCHEMA_FILES if t not in PHOTO_TABLES + USER_TABLES)


_REMOTE_FOLDERS: dict[str, frozenset[str] | None] = {}


def remote_folders(url: str) -> frozenset[str] | None:
    """The top-level folders of a Hugging Face lake, listed once per process and again after each publish; None if
    they couldn't be listed. A table added after the dataset was made has no folder until a publish sends its schema
    file, and DuckDB fails with a 404 on a glob over a folder that doesn't exist."""
    if url not in _REMOTE_FOLDERS:
        from huggingface_hub import HfApi
        from huggingface_hub.hf_api import RepoFolder

        repo, _, revision = url.removeprefix("hf://datasets/").partition("@")
        try:
            tree = HfApi(token=os.environ.get("HF_TOKEN")).list_repo_tree(
                repo, repo_type="dataset", revision=revision or None
            )
            _REMOTE_FOLDERS[url] = frozenset(f.path for f in tree if isinstance(f, RepoFolder))
        except Exception as e:  # noqa: BLE001 - unknown: read every remote glob, as before
            log.warning("listing %s failed (%s); reading every table remotely", url, e)
            return None
    return _REMOTE_FOLDERS[url]


def no_files(e: Exception) -> bool:
    """A read failed only because a table has no files yet: DuckDB's own "No files found", or, on a Hugging Face
    dataset, a 404 listing a folder no file was ever published to (a table added after the dataset was made)."""
    msg = str(e)
    return "No files found" in msg or ("error listing" in msg and "404" in msg)


# What a dropped connection to the remote lake looks like in DuckDB's httpfs error text (a NAT out of ports, a
# connection reset, a timeout, a 5xx). A missing file or a 4xx is never transient.
TRANSIENT_MARKERS = ("could not connect", "connection", "timed out", "timeout", "reset by peer", "http 5")
RETRY_WAITS = (2.0, 6.0, 18.0)  # seconds before each retry of a remote read at a job's start


def transient(e: Exception) -> bool:
    """A remote read failed on the connection, not on the data: worth a short retry."""
    msg = str(e).lower()
    return not no_files(e) and any(m in msg for m in TRANSIENT_MARKERS)


def retry_transient(fn, *args, **kwargs):
    """fn(*args, **kwargs), retried after RETRY_WAITS on a transient connection error (`transient`). The jobs'
    first reads of the hf:// lake (the registry state, what this run already fetched) go through it, so one dropped
    connection doesn't fail a whole crawl before its first request."""
    for i, wait in enumerate((*RETRY_WAITS, None)):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if wait is None or not transient(e):
                raise
            log.warning(
                "remote lake read failed (%s); retry %d of %d in %.0fs", e, i + 1, len(RETRY_WAITS), wait
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


def _same_file(info, path: Path) -> bool:
    """Whether a dataset file (a get_paths_info entry) has a local file's content: by its LFS sha256, or else its
    git blob id. A file whose hash isn't known counts as the same, so it's never replaced on a guess."""
    data = path.read_bytes()
    lfs = getattr(info, "lfs", None)
    if lfs:
        return lfs["sha256"] == hashlib.sha256(data).hexdigest()
    blob_id = getattr(info, "blob_id", None)
    return blob_id is None or blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _field_paths(schema: pa.Schema) -> frozenset[str]:
    """Every column and nested field by dotted path (a struct's fields, and those of a list's struct items), so a
    field added inside a struct, like snapshots' variants.currency, counts as an added column."""

    def walk(prefix: str, t: pa.DataType):
        if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
            yield from walk(prefix, t.value_type)
        elif pa.types.is_struct(t):
            for f in t:
                yield f"{prefix}.{f.name}"
                yield from walk(f"{prefix}.{f.name}", f.type)

    return frozenset(path for f in schema for path in (f.name, *walk(f.name, f.type)))


def _stale_schema(api, repo_id: str, rel: str, info, path: Path) -> bool:
    """Whether the dataset's schema file at `rel` has fewer columns (or nested fields) than the staged one at
    `path`: only then is it replaced. Identical bytes skip the download; other bytes (another pyarrow's created_by)
    compare by name."""
    if _same_file(info, path):
        return False
    remote = pq.read_schema(api.hf_hub_download(repo_id, rel, repo_type="dataset"))
    return _field_paths(remote) < _field_paths(pq.read_schema(path))


def new_batch_id(now: datetime | None = None) -> str:
    """Sortable and unique: re-running a step adds a new batch instead of rewriting an old one."""
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


class ReadOnlyLake(PermissionError):
    """A write was asked of a Lake opened read-only (Lake(read_only=True)): the admin's lake, which only reads."""


class Lake:
    def __init__(
        self,
        url: str | None = None,
        staging: str | Path | None = None,
        tables: tuple[str, ...] | None = None,
        read_only: bool = False,
    ):
        """`read_only`: a reader's lake (the admin's). Nothing is created or written, not even the schema files
        ensure_schemas would add, and every write method raises ReadOnlyLake. A remote one reads only the remote
        lake, never a staging folder."""
        self.url = (url or os.environ.get("COFFEEDATA_LAKE") or DEFAULT_URL).rstrip("/")
        self.tables = tables or LAKE_TABLES
        self.remote = self.url.startswith("hf://")
        self.read_only = read_only
        self.root = (
            Path(staging or os.environ.get("COFFEEDATA_STAGING", "build/lake-staging"))
            if self.remote
            else Path(self.url)
        )
        self._publishing = threading.Lock()
        self._schemas_checked = False
        if not read_only:
            self.ensure_schemas()

    def _writable(self) -> None:
        if self.read_only:
            raise ReadOnlyLake(f"{self.url} is open read-only")

    @classmethod
    def photos(cls) -> Lake:
        """The private photo dataset: COFFEEDATA_PHOTOS, or ./lake-photos."""
        url = os.environ.get("COFFEEDATA_PHOTOS") or "lake-photos"
        return cls(url, staging="build/photos-staging", tables=PHOTO_TABLES)

    @classmethod
    def users(cls) -> Lake:
        """The private users dataset: COFFEEDATA_USERS, or ./lake-users. Never a dbt source."""
        url = os.environ.get("COFFEEDATA_USERS") or "lake-users"
        return cls(url, staging="build/users-staging", tables=USER_TABLES)

    # --- writing -------------------------------------------------------------------------------------

    def ensure_schemas(self) -> None:
        self._writable()
        for table in self.tables:
            p = self.root / SCHEMA_FILES[table]
            # A zero-row schema file from before a column was added is refreshed in place, so globs always see
            # every column, nested struct fields included (a glob's union_by_name never adds a nested field that no
            # file has). That is the pattern for added columns; a changed type or meaning still gets a new schema
            # file beside the old one (like shots' schema-v2), since names are all this compares (Parquet stores
            # timestamp[s] as [ms], so comparing full schemas would never match).
            if not p.exists() or _field_paths(pq.read_schema(p)) != _field_paths(SCHEMAS[table]):
                p.parent.mkdir(parents=True, exist_ok=True)
                # A unique temp file in the same folder: two Lake()s refreshing at once never share one, and a
                # reader globbing *.parquet never sees a half-written file.
                fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".schema-", suffix=".tmp")
                os.close(fd)
                pq.write_table(SCHEMAS[table].empty_table(), tmp)
                # mkstemp makes it owner-only; schema files are read like any lake file. GCSFuse (Cloud Run's
                # /mnt/staging) refuses chmod, and its mount's file-mode applies there instead.
                with contextlib.suppress(OSError):
                    os.chmod(tmp, 0o644)
                os.replace(tmp, p)

    def write(self, table: str, rel: str, rows: list[dict]) -> Path | None:
        """Write rows as a new Parquet file. Refuses to replace an existing file (immutability)."""
        self._writable()
        if not rows:
            return None
        path = self.root / rel
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"path escapes the lake: {rel}")
        if path.exists():
            raise FileExistsError(f"lake files are immutable: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        schema = SCHEMAS[table]
        tbl = pa.Table.from_pylist([{f.name: r.get(f.name) for f in schema} for r in rows], schema=schema)
        tmp = path.with_suffix(".tmp")
        pq.write_table(tbl, tmp, compression="zstd")
        tmp.rename(path)
        return path

    def write_partition(self, table: str, run_date: str, batch_id: str, rows: list[dict]) -> Path | None:
        if not rows:
            return None
        prefix = f"enrichments/method={rows[0]['method']}" if table == "enrichments" else table
        return self.write(table, f"{prefix}/run_date={run_date}/batch={batch_id}.parquet", rows)

    # --- reading -------------------------------------------------------------------------------------

    def sources(self, table: str) -> list[str]:
        """Globs DuckDB should read for a table: local (staged) files, plus the remote lake if there is one."""
        if self.read_only and self.remote:  # nothing is staged by a reader: the remote lake only
            return [f"{self.url}/{GLOBS[table]}"]
        out = [str(self.root / GLOBS[table])]
        if self.remote and self.remote_has(table):
            out.append(f"{self.url}/{GLOBS[table]}")
        return out

    def remote_has(self, table: str) -> bool:
        """Whether the remote lake has this table's folder yet (assumed so if that can't be listed)."""
        folders = remote_folders(self.url)
        return folders is None or GLOBS[table].split("/")[0] in folders

    def scan(self, table: str) -> str:
        """SQL table expression over every file of a table."""
        files = ", ".join(f"'{s}'" for s in self.sources(table))
        return f"read_parquet([{files}], union_by_name = true, hive_partitioning = false)"

    def latest(self, table: str, key: str, attempts: int = 1) -> list[dict]:
        """The latest record per key (by logged_at): corrections are new records, so this is current state.

        A remote lake made before a table existed has no folder for it until the next publish adds the schema
        file; until then the table is read from staging alone rather than failing. Any other failed read (a 429,
        a timeout) is tried `attempts` times in all, 2, 4 … seconds apart, before it's raised."""
        sql = "select * from {} where {} is not null qualify row_number() over (partition by {} order by logged_at desc) = 1"
        for attempt in range(attempts):
            try:
                cur = self.connect().execute(sql.format(self.scan(table), key, key))
                break
            except Exception as e:  # noqa: BLE001 - duckdb.IOException; duckdb is imported lazily
                if self.remote and no_files(e):
                    local = f"read_parquet(['{self.root / GLOBS[table]}'], union_by_name = true, hive_partitioning = false)"
                    cur = self.connect().execute(sql.format(local, key, key))
                    break
                if attempt == attempts - 1:
                    raise
                log.warning("reading %s failed (%s); retrying", table, type(e).__name__)
                time.sleep(2 ** (attempt + 1))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def files_after(self, table: str, after: datetime) -> list[str]:
        """Files of a per-record table (`<id>__<YYYYmmddTHHMMSS>.parquet`) written after a time, from the
        file names alone: one directory listing instead of one request per file."""
        stamp = f"{after.astimezone(UTC):%Y%m%dT%H%M%S}"
        prefix = GLOBS[table].split("/")[0]

        def newer(name: str) -> bool:
            _, sep, at = name.removesuffix(".parquet").rpartition("__")
            return bool(sep) and at > stamp

        out = sorted(str(p) for p in (self.root / prefix).glob("*.parquet") if newer(p.name))
        if self.remote:
            from huggingface_hub import HfApi

            repo_id = self.url.removeprefix("hf://datasets/").split("@")[0]
            api = HfApi(token=os.environ.get("HF_TOKEN"))
            for f in api.list_repo_tree(repo_id, path_in_repo=prefix, repo_type="dataset"):
                if f.path.endswith(".parquet") and newer(f.path.rsplit("/", 1)[-1]):
                    out.append(f"{self.url}/{f.path}")
        return out

    def mart_sources(self, name: str) -> list[str]:
        """The weekly derived export of a dbt mart (marts/<name>.parquet), local first."""
        local = self.root / "marts" / f"{name}.parquet"
        if (local.exists() and not (self.read_only and self.remote)) or not self.remote:
            return [str(local)]
        return [f"{self.url}/marts/{name}.parquet"]

    def connect(self, database: str = ":memory:", read_only: bool = False):
        import duckdb

        con = duckdb.connect(database, read_only=read_only)
        if self.remote:
            con.execute("INSTALL httpfs; LOAD httpfs;")
            token = os.environ.get("HF_TOKEN")
            if token:
                con.execute("CREATE OR REPLACE SECRET hf (TYPE huggingface, TOKEN ?)", [token])
        return con

    # --- publishing ----------------------------------------------------------------------------------

    def staged_files(self) -> list[Path]:
        """Staged files of this lake's own tables (and its marts) only. Each dataset stages in its own folder, and
        this makes it structural: even a staging folder set to overlap another can't publish password hashes or
        photos into the main dataset."""
        own = {GLOBS[t].split("/")[0] for t in self.tables} | (
            {"marts", "releases"} if self.tables == LAKE_TABLES else set()
        )
        return sorted(
            p
            for p in self.root.rglob("*.parquet")
            if p.is_file() and p.relative_to(self.root).parts[0] in own
        )

    def write_mart(self, name: str, con, sql: str) -> Path:
        """Export a derived table (replaced every run; the raw tables are the source of truth)."""
        self._writable()
        path = self.root / "marts" / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
        return path

    def write_release(self, name: str, run_date: str, build_id: str, con, sql: str) -> Path:
        """One table of a build's release: immutable, so any release can be served again or diffed later."""
        self._writable()
        path = self.root / "releases" / f"run_date={run_date}" / f"build={build_id}" / f"{name}.parquet"
        if path.exists():
            raise FileExistsError(f"lake files are immutable: {path.relative_to(self.root)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
        return path

    def write_ops(self, name: str, run_date: str, build_id: str, table: pa.Table) -> Path:
        """One of a build's ops tables (quality.OPS_TABLES), beside its release under ops/: immutable like it."""
        self._writable()
        path = (
            self.root / "releases" / f"run_date={run_date}" / f"build={build_id}" / "ops" / f"{name}.parquet"
        )
        if path.exists():
            raise FileExistsError(f"lake files are immutable: {path.relative_to(self.root)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(path)
        return path

    def publish(self, message: str, attempts: int = 5, schemas: bool = False) -> int:
        """Upload staged files to the remote lake in one commit, then clear the staging area.

        A file already in the dataset is never replaced (except the derived marts/). Staged paths carry a batch
        id unique to the run that wrote them, so one that's already there is this run's own upload, from a
        commit whose response was lost or a process that died before clearing staging, and it's skipped.
        Retries use exponential backoff (2, 4, 8, 16 s). If every attempt fails, the staged files stay on disk
        and the next publish sends them.

        A table's zero-row schema file is sent when the dataset doesn't have it yet (a table added after the lake
        was created), so a glob over that table resolves, and replaced when the dataset's copy lacks columns the
        staged one has (one from before a column was added, which ensure_schemas refreshed in staging), so a
        reader of the dataset alone sees every column. Like ensure_schemas, that compares column names, so a
        process running older code (fewer columns) never replaces a newer schema file. This is checked once per
        process, or every time with `schemas`. One publish runs at a time per lake, so writers can keep staging files meanwhile.
        """
        self._writable()
        if not self.remote:
            return 0
        with self._publishing:
            return self._publish(message, attempts, schemas)

    def _publish(self, message: str, attempts: int, schemas: bool) -> int:
        schema_paths = {SCHEMA_FILES[t] for t in self.tables}
        check_schemas = schemas or not self._schemas_checked
        files = [
            p
            for p in self.staged_files()
            if check_schemas or p.relative_to(self.root).as_posix() not in schema_paths
        ]
        if not files:
            return 0  # nothing new: no API calls
        from huggingface_hub import CommitOperationAdd, HfApi

        repo_id = self.url.removeprefix("hf://datasets/").split("@")[0]
        api = HfApi(token=os.environ.get("HF_TOKEN"))
        rels = [p.relative_to(self.root).as_posix() for p in files]
        for attempt in range(attempts):
            try:
                # Only the staged paths are looked up, so the cost doesn't grow with the dataset.
                existing = {
                    f.path: f
                    for i in range(0, len(rels), 500)
                    for f in api.get_paths_info(repo_id, rels[i : i + 500], repo_type="dataset")
                }
                # A job sharing this staging folder (the confirmation run's concurrent arms) may have sent and
                # removed a file since it was listed: it is in the dataset now, or goes with that job's commit.
                ops = [
                    CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(p))
                    for p, rel in zip(files, rels, strict=True)
                    if p.exists()
                    and (
                        rel not in existing
                        or rel.startswith("marts/")
                        or (rel in schema_paths and _stale_schema(api, repo_id, rel, existing[rel], p))
                    )
                ]
                if ops:
                    api.create_commit(
                        repo_id=repo_id, repo_type="dataset", operations=ops, commit_message=message
                    )
                break
            except Exception as e:  # noqa: BLE001 - network, rate limit or server error: try again
                if attempt == attempts - 1:
                    raise
                log.warning("publish attempt %d failed (%s); retrying", attempt + 1, e)
                time.sleep(2 ** (attempt + 1))
        self._schemas_checked = True
        _REMOTE_FOLDERS.pop(self.url, None)  # new folders may exist now
        for p, rel in zip(files, rels, strict=True):
            if rel not in schema_paths:
                p.unlink(missing_ok=True)
        return len(ops)
