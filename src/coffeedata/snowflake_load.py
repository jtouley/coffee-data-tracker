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


def _rows(cur, sql: str) -> list[dict[str, str]]:
    """A SHOW command's rows as {lower-case column: upper-case value}."""
    rows = cur.execute(sql).fetchall()
    cols = [d[0].lower() for d in cur.description or []]
    return [{k: str(v).upper() for k, v in zip(cols, r, strict=False)} for r in rows]


def _visible(cur, schema: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Every table or view in a schema that this role can see: its sorted upper-case columns and its owning role,
    by lower-case name. information_schema lists only objects the role has a privilege on, so an object another role
    owns (one recreated as ACCOUNTADMIN) isn't here at all."""
    columns: dict[str, list[str]] = {}
    for name, col in cur.execute(
        f"select table_name, column_name from information_schema.columns where table_schema = '{schema}'"
    ).fetchall():
        columns.setdefault(str(name).lower(), []).append(str(col).upper())
    owners = {
        str(name).lower(): str(owner or "").upper()
        for name, owner in cur.execute(
            f"select table_name, table_owner from information_schema.tables where table_schema = '{schema}'"
        ).fetchall()
    }
    return {k: sorted(v) for k, v in columns.items()}, owners


def _no_access(e: Exception) -> bool:
    msg = str(e).lower()
    return "does not exist or not authorized" in msg or "insufficient privileges" in msg


def _share_views(cur, share: str) -> set[str] | None:
    """The views granted to the share, by lower-case name, from the share's own grants: its owner (COFFEE_LOADER,
    which created it in admin_setup.sql part 2) lists them whoever owns the views. None if this role can't see the
    share: it doesn't exist yet, or another role created it."""
    try:
        rows = _rows(cur, f"show grants to share {share}")
    except Exception as e:  # noqa: BLE001 - snowflake.connector's ProgrammingError
        if not _no_access(e):
            raise
        return None
    return {r["name"].rsplit(".", 1)[-1].strip('"').lower() for r in rows if r.get("granted_on") == "VIEW"}


def plan(cur, export_dir: Path, share: str, migrate: bool) -> dict:
    """What a load (or a migration) would do, from read-only queries: the tables whose columns differ from the export,
    each share view's state, the grants it needs, and every reason to refuse (`problems`; nothing is changed if
    there are any). A view's state is ok, stale (its columns differ), new (it doesn't exist and isn't in the share:
    a public table added since admin_setup.sql part 2), or, only for a migration, hidden (granted to the share
    but invisible to this role, so another role owns it)."""
    import pyarrow.parquet as pq

    role = str(cur.execute("select current_role()").fetchone()[0]).upper()
    want = {
        t: sorted(c.upper() for c in pq.read_schema(export_dir / f"{t}.parquet").names) for t in PUBLIC_TABLES
    }
    tables, table_owners = _visible(cur, SCHEMA)
    views, view_owners = _visible(cur, "SHARE")
    problems = []
    for t in PUBLIC_TABLES:
        if t in table_owners and table_owners[t] != role:
            problems.append(
                f"{SCHEMA}.{t} is owned by {table_owners[t]}, not {role}, so {role} can't reload it. Its owner (or "
                f"ACCOUNTADMIN) can give it back: grant ownership on table {SCHEMA}.{t} to role {role} copy current "
                "grants"
            )
    changed = [t for t in PUBLIC_TABLES if t in tables and tables[t] != want[t]]
    state = {}
    for t in PUBLIC_TABLES:
        v = SHARE_VIEWS[t]
        state[t] = "new" if v not in views else "stale" if views[v] != want[t] else "ok"
    shared = _share_views(cur, share) if migrate else None
    grants: list[str] = []
    if migrate:
        if shared is None and views:
            problems.append(
                f"the share views exist but {role} can't see share {share}: it doesn't exist under that name (set "
                "SNOWFLAKE_SHARE) or another role created it, so the recreated views might leave it"
            )
        for t in PUBLIC_TABLES:
            v = SHARE_VIEWS[t]
            if v in views and view_owners.get(v, role) != role:
                problems.append(
                    f"SHARE.{v} is owned by {view_owners[v]}, not {role}, so {role} can't recreate it. Its owner (or "
                    f"ACCOUNTADMIN) can give it back, keeping its grants: grant ownership on view SHARE.{v} to role "
                    f"{role} copy current grants"
                )
            elif v not in views and shared is not None and v in shared:
                state[t] = "hidden"
                problems.append(
                    f"SHARE.{v} is in share {share} but {role} can't see it, so another role (ACCOUNTADMIN?) owns it. "
                    f"Its owner (or ACCOUNTADMIN) can give it back, keeping its grants: grant ownership on view "
                    f"SHARE.{v} to role {role} copy current grants"
                )
            elif shared is not None and v not in shared:
                grants.append(t)  # a view this role owns, or a new one it creates: granted to the share
    elif changed or any(state[t] == "stale" for t in PUBLIC_TABLES):
        names = changed + [f"SHARE.{SHARE_VIEWS[t]}" for t in PUBLIC_TABLES if state[t] == "stale"]
        problems.append(
            f"{', '.join(names)} in Snowflake have different columns from the export: the public contract changed. "
            "Run `coffeedata load-snowflake --migrate` (or the weekly workflow with only `snowflake_migrate` set) "
            "to recreate those tables and the share views"
        )
    return {"role": role, "share": share if shared is not None else None, "changed": changed, "views": state,
            "grants": grants, "problems": problems}  # fmt: skip


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


def load(export_dir: Path, con=None, migrate: bool = False, dry_run: bool = False) -> dict:
    """Load one export. Everything is checked first, from read-only queries (`plan`), and nothing changes if any
    check fails, so a mismatch never leaves Snowflake half on the new export. If a table's or share view's columns
    differ from the export (a new public contract version), nothing is loaded unless `migrate`: then the files are
    uploaded, the differing tables are dropped and loaded fresh, every share view is recreated with `copy grants`
    (so none leaves the share) and a missing one created and granted. Each step can be repeated, so a migration
    that failed partway is finished by running it again. `dry_run` returns the plan and the statements it would
    run, and changes nothing."""
    manifest = json.loads((export_dir / "manifest.json").read_text())
    exported = re.sub(r"[^0-9T]", "", manifest["exported_at"].split("+")[0]) + "Z"  # 20261027T020148Z
    run_path = f"run={manifest['as_of_run']}/exported={exported}"
    share = os.environ.get("SNOWFLAKE_SHARE", "COFFEE_DATA_SHARE").upper()
    con = con or connect()
    cur = con.cursor()
    try:
        p = plan(cur, export_dir, share, migrate)
        views = [t for t, s in p["views"].items() if migrate and s in ("ok", "stale", "new")]
        if migrate and p["share"] is None and not any(s != "new" for s in p["views"].values()):
            views = []  # no share and no views yet: admin_setup.sql part 2 creates them
        setup = [
            f"create schema if not exists {SCHEMA}",
            f"create file format if not exists {SCHEMA}.PARQUET_FF type = parquet",
            f"create stage if not exists {SCHEMA}.{STAGE} file_format = {SCHEMA}.PARQUET_FF "
            "comment = 'Weekly facts-only exports, one folder per run'",
        ]
        # Granted first, to the view as it is, so a grant that fails changes nothing; `copy grants` keeps it.
        early = [f"grant select on view SHARE.{SHARE_VIEWS[t]} to share {share}"
                 for t in p["grants"] if p["views"][t] != "new"]  # fmt: skip
        puts = [
            f"put 'file://{(export_dir / f'{t}.parquet').resolve()}' @{SCHEMA}.{STAGE}/{run_path}/ "
            "auto_compress = false overwrite = false"
            for t in PUBLIC_TABLES
        ]
        # Every table is created (if it isn't there) before any is dropped or truncated: one that exists but this
        # role can't see fails here, before anything is loaded.
        creates = [statements(t, run_path)[0] for t in PUBLIC_TABLES]
        drops = [f"drop table if exists {SCHEMA}.{t}" for t in p["changed"]] if migrate else []

        def view(t: str) -> str:
            return f"create or replace secure view SHARE.{SHARE_VIEWS[t]} copy grants as select * from {SCHEMA}.{t}"

        # A new view is created and granted as soon as its table exists, before anything is dropped or loaded: one
        # that does exist, owned by another role and outside the share, fails here rather than after the reload.
        new = [t for t in views if p["views"][t] == "new"]
        fresh = [view(t) for t in new] + [
            f"grant select on view SHARE.{SHARE_VIEWS[t]} to share {share}" for t in new if t in p["grants"]
        ]
        late = [view(t) for t in views if t not in new or t in p["changed"]]
        if dry_run:
            reloads = [sql for t in PUBLIC_TABLES for sql in statements(t, run_path)]
            return p | {"statements": setup + early + puts + creates + fresh + drops + reloads + late}
        if p["problems"]:
            raise RuntimeError("Nothing was loaded or changed. " + " ".join(p["problems"]))
        for sql in setup + early + puts:
            cur.execute(sql)
        for t, sql in zip(PUBLIC_TABLES, creates, strict=True):
            try:
                cur.execute(sql)
            except Exception as e:  # noqa: BLE001 - snowflake.connector's ProgrammingError
                raise RuntimeError(
                    f"{SCHEMA}.{t} couldn't be created ({e}). If it exists, another role owns it and this role can't "
                    f"see it: its owner (or ACCOUNTADMIN) can give it back with grant ownership on table {SCHEMA}.{t} "
                    f"to role {p['role']} copy current grants. Nothing was loaded."
                ) from e
        for sql in fresh + drops:
            cur.execute(sql)
        counts = {}
        for table in PUBLIC_TABLES:
            for sql in statements(table, run_path):
                cur.execute(sql)
            counts[table] = cur.execute(f"select count(*) from {SCHEMA}.{table}").fetchone()[0]
        for sql in late:
            cur.execute(sql)
    finally:
        cur.close()
        con.close()
    mismatched = {
        t: (counts[t], manifest["tables"][t]) for t in PUBLIC_TABLES if counts[t] != manifest["tables"][t]
    }
    if mismatched:
        raise RuntimeError(f"row counts differ from the export (snowflake, export): {mismatched}")
    return {"run": manifest["as_of_run"], "tables": counts}
