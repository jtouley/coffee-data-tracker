"""Load the facts-only public tables into Snowflake through an internal stage (no S3 needed).

Credentials come from the environment, for a key-pair service user:
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY (PEM, unencrypted PKCS#8)
    optional: SNOWFLAKE_ROLE (COFFEE_LOADER), SNOWFLAKE_WAREHOUSE (COFFEE_WH), SNOWFLAKE_DATABASE (COFFEE_DATA)

Each run uploads the export to @<schema>.public_export/run=<as_of_run>/. Those files are never overwritten, so
the stage keeps every weekly export. Then each table is replaced in one transaction, and row counts are checked
against the export manifest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .public import PUBLIC_TABLES

SCHEMA = "MARKET"
STAGE = "PUBLIC_EXPORT"


def private_key_der(pem: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def connect():
    import snowflake.connector

    missing = [v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"missing environment: {', '.join(missing)}")
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
        private_key=private_key_der(os.environ["SNOWFLAKE_PRIVATE_KEY"]),
        role=os.environ.get("SNOWFLAKE_ROLE", "COFFEE_LOADER"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COFFEE_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "COFFEE_DATA"),
        application="coffee-data-tracker",
    )


def statements(table: str, run_path: str) -> list[str]:
    """SQL to (create and) reload one table from this run's staged file, as one transaction."""
    src = f"@{SCHEMA}.{STAGE}/{run_path}/{table}.parquet"
    return [
        f"create table if not exists {SCHEMA}.{table} using template (select array_agg(object_construct(*)) "
        f"from table(infer_schema(location => '{src}', file_format => '{SCHEMA}.PARQUET_FF')))",
        "begin",
        f"truncate table {SCHEMA}.{table}",
        f"copy into {SCHEMA}.{table} from {src} file_format = (format_name = '{SCHEMA}.PARQUET_FF') "
        "match_by_column_name = case_insensitive force = true on_error = abort_statement",
        "commit",
    ]


def load(export_dir: Path, con=None) -> dict:
    manifest = json.loads((export_dir / "manifest.json").read_text())
    run_path = f"run={manifest['as_of_run']}"
    con = con or connect()
    cur = con.cursor()
    try:
        cur.execute(f"create schema if not exists {SCHEMA}")
        cur.execute(f"create file format if not exists {SCHEMA}.PARQUET_FF type = parquet")
        cur.execute(f"create stage if not exists {SCHEMA}.{STAGE} file_format = {SCHEMA}.PARQUET_FF "
                    "comment = 'Weekly facts-only exports, one folder per run'")
        for table in PUBLIC_TABLES:
            f = (export_dir / f"{table}.parquet").resolve()
            cur.execute(f"put 'file://{f}' @{SCHEMA}.{STAGE}/{run_path}/ auto_compress = false overwrite = false")
        counts = {}
        for table in PUBLIC_TABLES:
            for sql in statements(table, run_path):
                cur.execute(sql)
            counts[table] = cur.execute(f"select count(*) from {SCHEMA}.{table}").fetchone()[0]
    finally:
        cur.close()
        con.close()
    mismatched = {t: (counts[t], manifest["tables"][t]) for t in PUBLIC_TABLES if counts[t] != manifest["tables"][t]}
    if mismatched:
        raise RuntimeError(f"row counts differ from the export (snowflake, export): {mismatched}")
    return {"run": manifest["as_of_run"], "tables": counts}
