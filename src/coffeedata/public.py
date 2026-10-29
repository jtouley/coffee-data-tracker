"""Export the facts-only public tables (dbt models `public_*`) for release.

Destinations:
  * a local directory (default build/public)
  * hf://datasets/<owner>/<name>: a separate, public Hugging Face dataset (one commit per export)
  * s3://bucket/prefix or gs://bucket/prefix: object storage that Snowflake and Databricks load from.
    S3 uses DuckDB's credential chain (AWS env vars, profile or instance role); GCS uses HMAC keys from
    GCS_HMAC_KEY_ID / GCS_HMAC_SECRET.

Current tables are replaced each week (they are derived); history lives in `public_price_event`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

PUBLIC_TABLES = ["public_coffee", "public_roaster", "public_price_event", "public_market_weekly"]
# The order each file is written in, ending in the table's key so it is total: the same release always gives the
# same file, and readers of public_price_event can skip row groups by run_date.
ORDER = {
    "public_coffee": "coffee_id",
    "public_roaster": "roaster_id",
    "public_price_event": "run_date, coffee_id, variant_id, event",
    "public_market_weekly": "coffees desc, origin_country, process",  # the biggest markets first, as the model
}
CARD = Path("docs/PUBLIC_DATASET_CARD.md")
HF_DATA_DIR = "data"  # where the tables go in a Hugging Face export


def hf_repo_id(url: str) -> str:
    """`hf://datasets/<owner>/<name>[@rev][/path]` -> `<owner>/<name>`."""
    return "/".join(url.removeprefix("hf://datasets/").split("@")[0].strip("/").split("/")[:2])


def export(db: Path, dest: str = "build/public") -> dict:
    import duckdb

    con = duckdb.connect(str(db), read_only=True)

    def scalar(sql: str):
        row = con.execute(sql).fetchone()
        return row[0] if row else None

    counts = {t: scalar(f"select count(*) from {t}") for t in PUBLIC_TABLES}
    as_of = scalar("select max(last_seen) from public_coffee")
    manifest = {
        "as_of_run": str(as_of) if as_of else None,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tables": counts,
    }

    if dest.startswith(("s3://", "gs://")):
        _configure_object_store(con, dest)
        for t in PUBLIC_TABLES:
            con.execute(
                f"COPY (select * from {t} order by {ORDER[t]}) TO '{dest.rstrip('/')}/{t}.parquet' "
                "(FORMAT parquet, COMPRESSION zstd)"
            )
        return manifest | {"dest": dest}

    local = Path("build/public") if dest.startswith("hf://") else Path(dest)
    local.mkdir(parents=True, exist_ok=True)
    for t in PUBLIC_TABLES:
        con.execute(
            f"COPY (select * from {t} order by {ORDER[t]}) TO '{local / (t + '.parquet')}' "
            "(FORMAT parquet, COMPRESSION zstd)"
        )
    (local / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    if dest.startswith("hf://"):
        _upload_hf(local, dest, manifest)
    return manifest | {"dest": dest}


def _configure_object_store(con, dest: str) -> None:
    con.execute("INSTALL httpfs; LOAD httpfs;")
    if dest.startswith("s3://"):
        con.execute("INSTALL aws; LOAD aws;")
        con.execute("CREATE OR REPLACE SECRET pub (TYPE s3, PROVIDER credential_chain)")
    else:
        con.execute(
            "CREATE OR REPLACE SECRET pub (TYPE gcs, KEY_ID ?, SECRET ?)",
            [os.environ["GCS_HMAC_KEY_ID"], os.environ["GCS_HMAC_SECRET"]],
        )


def _upload_hf(local: Path, dest: str, manifest: dict) -> None:
    from huggingface_hub import CommitOperationAdd, HfApi

    repo_id = hf_repo_id(dest)
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    api.create_repo(repo_id, repo_type="dataset", private=False, exist_ok=True)
    ops = [
        CommitOperationAdd(path_in_repo=f"{HF_DATA_DIR}/{p.name}", path_or_fileobj=str(p))
        for p in sorted(local.glob("*.parquet"))
    ]
    ops.append(CommitOperationAdd(path_in_repo="manifest.json", path_or_fileobj=str(local / "manifest.json")))
    if CARD.exists():
        ops.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=str(CARD)))
    api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=ops,
        commit_message=f"weekly public export (run {manifest['as_of_run']})",
    )
