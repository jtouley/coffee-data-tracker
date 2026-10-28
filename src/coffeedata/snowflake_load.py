"""Load the facts-only public tables into Snowflake through an internal stage (no S3 needed).

Credentials come from the environment, for a key-pair service user:
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY (PEM, unencrypted PKCS#8)
    optional: SNOWFLAKE_ROLE (COFFEE_LOADER), SNOWFLAKE_WAREHOUSE (COFFEE_WH), SNOWFLAKE_DATABASE (COFFEE_DATA)

Each export is uploaded to @<schema>.public_export/run=<as_of_run>/exported=<time>/. Those files are never
overwritten, so the stage keeps every export, and re-exporting the same week loads the new files, not the old. Then each table is replaced in one transaction, and row counts are checked
against the export manifest.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .public import PUBLIC_TABLES

SCHEMA = "MARKET"
STAGE = "PUBLIC_EXPORT"
# The share's secure views over the loaded tables (marketplace/snowflake/admin_setup.sql, part 2).
SHARE_VIEWS = {
    "public_coffee": "coffee",
    "public_price_event": "price_event",
    "public_market_weekly": "market_weekly",
    "public_roaster": "roaster",
}


def private_key_der(value: str) -> bytes:
    """Accepts the .p8 file as PEM (with real or literal "\\n" line breaks) or as one base64 line of the
    whole file (`base64 -w0 coffee_rsa.p8`), since some secret fields don't keep line breaks."""
    import base64

    from cryptography.hazmat.primitives import serialization

    text = value.strip().replace("\\n", "\n")
    if not text.startswith("-----BEGIN"):
        decoded = base64.b64decode("".join(text.split()))
        if not decoded.startswith(b"-----BEGIN"):
            return decoded  # already DER
        text = decoded.decode()
    key = serialization.load_pem_private_key(text.encode(), password=None)
    return key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def connect():
    import snowflake.connector

    missing = [
        v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY") if not os.environ.get(v)
    ]
    if missing:
        raise SystemExit(f"missing environment: {', '.join(missing)}")
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key=private_key_der(os.environ["SNOWFLAKE_PRIVATE_KEY"]),
        role=os.environ.get("SNOWFLAKE_ROLE", "COFFEE_LOADER"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COFFEE_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "COFFEE_DATA"),
        application="coffee-data-tracker",
    )


def _columns(cur, schema: str, name: str) -> list[str]:
    """A table's or view's columns, sorted and upper case; [] if it doesn't exist."""
    return sorted(
        r[0].upper()
        for r in cur.execute(
            f"select column_name from information_schema.columns "
            f"where table_schema = '{schema}' and table_name = '{name.upper()}'"
        ).fetchall()
    )


def _view_grants(cur, view: str) -> tuple[str, set[str]]:
    """A view's owning role and the shares it's granted to. `show grants on view` works for the view's owner
    without any account privilege, unlike `show shares` (which needs IMPORT SHARE to list anything)."""
    rows = cur.execute(f"show grants on view {view}").fetchall()
    cols = [d[0].lower() for d in cur.description or []]
    owner, shares = "", set()
    for r in rows:
        g = {k: str(v).upper() for k, v in zip(cols, r, strict=False)}
        if g.get("privilege") == "OWNERSHIP":
            owner = g.get("grantee_name", "")
        elif g.get("granted_to") == "SHARE":
            shares.add(g.get("grantee_name", "").rsplit(".", 1)[-1])  # ORG.ACCOUNT.SHARE -> SHARE
    return owner, shares


def stale(cur, export_dir: Path) -> tuple[list[str], list[str]]:
    """Tables and share views whose columns differ from the export. COPY ... match_by_column_name silently loads
    NULL into a column the file no longer has, and a `select *` secure view keeps the columns it was created
    with, so neither can be used as is after a public contract change. One that doesn't exist yet doesn't
    differ: the load creates the table, and admin_setup.sql part 2 the view."""
    import pyarrow.parquet as pq

    tables, views = [], []
    for t in PUBLIC_TABLES:
        want = sorted(c.upper() for c in pq.read_schema(export_dir / f"{t}.parquet").names)
        if (have := _columns(cur, SCHEMA, t)) and have != want:
            tables.append(t)
        if (have := _columns(cur, "SHARE", SHARE_VIEWS[t])) and have != want:
            views.append(t)
    return tables, views


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


def load(export_dir: Path, con=None, migrate: bool = False) -> dict:
    """Load one export. If any table's or share view's columns differ from the export (a new public contract
    version), nothing is loaded unless `migrate`. Then the role must own the share views; the files are uploaded
    first, the differing tables are dropped and loaded fresh, and every share view is recreated with `copy grants`,
    so the share never loses one. Each step can be repeated, so a migration that failed partway is finished by
    running it again."""
    manifest = json.loads((export_dir / "manifest.json").read_text())
    exported = re.sub(r"[^0-9T]", "", manifest["exported_at"].split("+")[0]) + "Z"  # 20261027T020148Z
    run_path = f"run={manifest['as_of_run']}/exported={exported}"
    share = os.environ.get("SNOWFLAKE_SHARE", "COFFEE_DATA_SHARE").upper()
    con = con or connect()
    cur = con.cursor()
    try:
        cur.execute(f"create schema if not exists {SCHEMA}")
        cur.execute(f"create file format if not exists {SCHEMA}.PARQUET_FF type = parquet")
        cur.execute(
            f"create stage if not exists {SCHEMA}.{STAGE} file_format = {SCHEMA}.PARQUET_FF "
            "comment = 'Weekly facts-only exports, one folder per run'"
        )
        # Everything is checked before anything changes, so a mismatch never leaves Snowflake half on the new export.
        tables, views = stale(cur, export_dir)
        if (tables or views) and not migrate:
            raise RuntimeError(
                f"{', '.join(dict.fromkeys(tables + views))} in Snowflake have different columns from the export: "
                "the public contract changed. Nothing was loaded. Run `coffeedata load-snowflake --migrate` (or "
                "the weekly workflow with only `snowflake_migrate` set) to recreate those tables and the share views."
            )
        present = [t for t in PUBLIC_TABLES if migrate and _columns(cur, "SHARE", SHARE_VIEWS[t])]
        if present:  # checked first, so a migration that can't finish changes nothing
            role = str(cur.execute("select current_role()").fetchone()[0]).upper()
            grants = {t: _view_grants(cur, f"SHARE.{SHARE_VIEWS[t]}") for t in present}
            for t, (owner, _) in grants.items():
                if owner != role:
                    raise RuntimeError(
                        f"SHARE.{SHARE_VIEWS[t]} is owned by {owner}, not {role}, so {role} can't recreate it. "
                        f"Nothing was changed. Give it back, keeping its grants: grant ownership on view "
                        f"SHARE.{SHARE_VIEWS[t]} to role {role} copy current grants"
                    )
            # `copy grants` keeps each view's grant to the share, so only a view the share is missing (and the
            # share exists: another view is in it) is granted, before anything changes. That one grant needs the
            # share's owner; nothing else does, so a share handed to ACCOUNTADMIN for a listing is fine.
            if any(share in shares for _, shares in grants.values()):
                for t in (t for t, (_, shares) in grants.items() if share not in shares):
                    try:
                        cur.execute(f"grant select on view SHARE.{SHARE_VIEWS[t]} to share {share}")
                    except Exception as e:  # noqa: BLE001 - snowflake.connector's ProgrammingError
                        raise RuntimeError(
                            f"SHARE.{SHARE_VIEWS[t]} is missing from share {share}, and {role} can't grant it "
                            f"({e}): that needs the share's owner. Nothing was changed. Grant it as the owner: "
                            f"grant select on view SHARE.{SHARE_VIEWS[t]} to share {share}"
                        ) from e
        for table in PUBLIC_TABLES:  # uploaded before anything is dropped
            f = (export_dir / f"{table}.parquet").resolve()
            cur.execute(
                f"put 'file://{f}' @{SCHEMA}.{STAGE}/{run_path}/ auto_compress = false overwrite = false"
            )
        for table in tables:
            cur.execute(f"drop table if exists {SCHEMA}.{table}")
        counts = {}
        for table in PUBLIC_TABLES:
            for sql in statements(table, run_path):
                cur.execute(sql)
            counts[table] = cur.execute(f"select count(*) from {SCHEMA}.{table}").fetchone()[0]
        # Every existing view, not only the changed ones: one left over from a failed migration is rebuilt too.
        # Views that don't exist yet are left to admin_setup.sql part 2.
        for table in present:
            cur.execute(
                f"create or replace secure view SHARE.{SHARE_VIEWS[table]} copy grants as "
                f"select * from {SCHEMA}.{table}"
            )
    finally:
        cur.close()
        con.close()
    mismatched = {
        t: (counts[t], manifest["tables"][t]) for t in PUBLIC_TABLES if counts[t] != manifest["tables"][t]
    }
    if mismatched:
        raise RuntimeError(f"row counts differ from the export (snowflake, export): {mismatched}")
    return {"run": manifest["as_of_run"], "tables": counts}
