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

Bag photos go to their own private dataset (`Lake.photos()`), one photo per file:

    photos/<photo_id>.parquet                             the image, in Hugging Face's Image layout

Current state ("what's listed now", "is this roaster reachable", price history) is always *derived* from these
files, in dbt and in `state.py`, so re-running a step is safe: duplicates collapse on their natural keys.

For a remote lake (``hf://datasets/<owner>/<name>``) files are staged locally and published in one commit per
checkpoint (each ingest chunk and each weekly step).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
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

# Tables that live in the photo dataset rather than the main lake.
PHOTO_TABLES = ("photos",)

# Glob (relative to the lake root) that matches every file of a table, including its empty schema file.
GLOBS = {
    "snapshots": "snapshots/*/*.parquet",
    "bodies": "bodies/*/*.parquet",
    "fetches": "fetches/*/*.parquet",
    "probes": "probes/*/*.parquet",
    "registry": "registry/*/*.parquet",
    "enrichments": "enrichments/*/*/*.parquet",
    "llm_batches": "llm_batches/*/*.parquet",
    "shots": "shots/*.parquet",
    "equipment": "equipment/*.parquet",
    "bags": "bags/*.parquet",
    "photos": "photos/*.parquet",
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
    # v2 adds the app's columns. Old lakes keep their v1 file; this one is added beside it, so a glob over
    # only old shot files still has every column.
    "shots": "shots/shot_id=schema-v2.parquet",
    "equipment": "equipment/equipment_id=schema.parquet",
    "bags": "bags/bag_id=schema.parquet",
    "photos": "photos/photo_id=schema.parquet",
}
LAKE_TABLES = tuple(t for t in SCHEMA_FILES if t not in PHOTO_TABLES)


def new_batch_id(now: datetime | None = None) -> str:
    """Sortable and unique: re-running a step adds a new batch instead of rewriting an old one."""
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


class Lake:
    def __init__(
        self, url: str | None = None, staging: str | Path | None = None, tables: tuple[str, ...] | None = None
    ):
        self.url = (url or os.environ.get("COFFEEDATA_LAKE") or DEFAULT_URL).rstrip("/")
        self.tables = tables or LAKE_TABLES
        self.remote = self.url.startswith("hf://")
        self.root = (
            Path(staging or os.environ.get("COFFEEDATA_STAGING", "build/lake-staging"))
            if self.remote
            else Path(self.url)
        )
        self._publishing = threading.Lock()
        self._schemas_checked = False
        self.ensure_schemas()

    @classmethod
    def photos(cls) -> Lake:
        """The private photo dataset: COFFEEDATA_PHOTOS, or ./lake-photos."""
        url = os.environ.get("COFFEEDATA_PHOTOS") or "lake-photos"
        return cls(url, staging="build/photos-staging", tables=PHOTO_TABLES)

    # --- writing -------------------------------------------------------------------------------------

    def ensure_schemas(self) -> None:
        for table in self.tables:
            p = self.root / SCHEMA_FILES[table]
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(SCHEMAS[table].empty_table(), p)

    def write(self, table: str, rel: str, rows: list[dict]) -> Path | None:
        """Write rows as a new Parquet file. Refuses to replace an existing file (immutability)."""
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
        out = [str(self.root / GLOBS[table])]
        if self.remote:
            out.append(f"{self.url}/{GLOBS[table]}")
        return out

    def scan(self, table: str) -> str:
        """SQL table expression over every file of a table."""
        files = ", ".join(f"'{s}'" for s in self.sources(table))
        return f"read_parquet([{files}], union_by_name = true, hive_partitioning = false)"

    def latest(self, table: str, key: str) -> list[dict]:
        """The latest record per key (by logged_at): corrections are new records, so this is current state.

        A remote lake made before a table existed has no file for it until the next publish adds the schema
        file; until then the table is read from staging alone rather than failing."""
        sql = "select * from {} where {} is not null qualify row_number() over (partition by {} order by logged_at desc) = 1"
        try:
            cur = self.connect().execute(sql.format(self.scan(table), key, key))
        except Exception as e:  # noqa: BLE001 - duckdb.IOException; duckdb is imported lazily
            if not (self.remote and "No files found" in str(e)):
                raise
            local = f"read_parquet(['{self.root / GLOBS[table]}'], union_by_name = true, hive_partitioning = false)"
            cur = self.connect().execute(sql.format(local, key, key))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def mart_sources(self, name: str) -> list[str]:
        """The weekly derived export of a dbt mart (marts/<name>.parquet), local first."""
        local = self.root / "marts" / f"{name}.parquet"
        if local.exists() or not self.remote:
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
        return sorted(p for p in self.root.rglob("*.parquet") if p.is_file())

    def write_mart(self, name: str, con, sql: str) -> Path:
        """Export a derived table (replaced every run; the raw tables are the source of truth)."""
        path = self.root / "marts" / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
        return path

    def publish(self, message: str, attempts: int = 5, schemas: bool = False) -> int:
        """Upload staged files to the remote lake in one commit, then clear the staging area.

        A file already in the dataset is never replaced (except the derived marts/). Staged paths carry a batch
        id unique to the run that wrote them, so one that's already there is this run's own upload, from a
        commit whose response was lost or a process that died before clearing staging, and it's skipped.
        Retries use exponential backoff (2, 4, 8, 16 s). If every attempt fails, the staged files stay on disk
        and the next publish sends them.

        A table's zero-row schema file is sent when the dataset doesn't have it yet (a table added after the lake
        was created), so a glob over that table resolves; this is checked once per process, or every time with
        `schemas`. One publish runs at a time per lake, so writers can keep staging files meanwhile.
        """
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
                    f.path
                    for i in range(0, len(rels), 500)
                    for f in api.get_paths_info(repo_id, rels[i : i + 500], repo_type="dataset")
                }
                ops = [
                    CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(p))
                    for p, rel in zip(files, rels, strict=True)
                    if rel not in existing or rel.startswith("marts/")
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
        for p, rel in zip(files, rels, strict=True):
            if rel not in schema_paths:
                p.unlink()
        return len(ops)
