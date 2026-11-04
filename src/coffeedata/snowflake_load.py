"""Load the facts-only public tables into Snowflake through an internal stage (no S3 needed).

Credentials come from the environment, for a key-pair service user:
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY (PEM, unencrypted PKCS#8)
    optional: SNOWFLAKE_ROLE (COFFEE_LOADER), SNOWFLAKE_WAREHOUSE (COFFEE_WH), SNOWFLAKE_DATABASE (COFFEE_DATA)

Each export is uploaded to @<schema>.public_export/run=<as_of_run>/exported=<time>/. Those files are never
overwritten, so the stage keeps every export, and re-exporting the same week loads the new files, not the old. Then each table is replaced in one transaction, and row counts are checked
against the export manifest.

After the load, every table and share view gets its description and column descriptions from the public contract
(contracts/public-catalog.odcs.yaml, read at load time), so the listing's data dictionary is the contract's.

The tables keep the export's lower-case column names (infer_schema keeps the file's case, so they're quoted
identifiers). The share views, which are what Marketplace consumers query, rename them to upper case, so
consumers write `RELEASE_RUN_DATE` or `release_run_date` unquoted, as with any Snowflake table.
"""

from __future__ import annotations

import json
import os
import re
import sys
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
# The public ODCS contract, the source of every table, view and column description (the repository's copy, which
# the ingestion image also carries; from the working directory when the package is installed elsewhere).
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "public-catalog.odcs.yaml"


def contract_descriptions(path: Path | None = None) -> dict[str, tuple[str, dict[str, str]]]:
    """Each public table's description and its columns' descriptions (by lower-case name), from the contract."""
    import yaml

    path = path or (CONTRACT if CONTRACT.exists() else Path("contracts/public-catalog.odcs.yaml"))
    contract = yaml.safe_load(path.read_text())
    return {
        obj["name"]: (
            str(obj.get("description") or ""),
            {p["name"].lower(): str(p.get("description") or "") for p in obj.get("properties") or []},
        )
        for obj in contract["schema"]
    }


def sql_string(text: str) -> str:
    """A Snowflake string literal: whitespace collapsed to single spaces, backslashes and single quotes escaped."""
    return "'" + " ".join(text.split()).replace("\\", "\\\\").replace("'", "''") + "'"


def _ident(name: str) -> str:
    """A column name as Snowflake reports it, quoted so its case is kept (infer_schema keeps the file's case)."""
    return '"' + name.replace('"', '""') + '"'


# Snowflake's reserved keywords (docs: "Reserved & limited keywords"): a share view column named one of these
# would still need quotes, so none may be.
_RESERVED = """ACCOUNT ALL ALTER AND ANY AS ASOF BETWEEN BY CASE CAST CHECK COLUMN CONNECT CONNECTION
    CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DATABASE DELETE DISTINCT DROP ELSE
    EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT
    INTO IS ISSUE JOIN LATERAL LEFT LIKE LOCALTIME LOCALTIMESTAMP MATCH_CONDITION MINUS NATURAL NOT NULL OF ON OR
    ORDER ORGANIZATION QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE
    TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING VALUES VIEW WHEN WHENEVER WHERE WITH"""
RESERVED = frozenset(_RESERVED.split())


def view_column(name: str) -> str:
    """A share view's name for a column: upper case and unquoted, so consumers can write it in any case without
    quotes. Raises for a name that would still need quotes (not a plain identifier, or a reserved keyword)."""
    upper = name.upper()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_$]*", upper) or upper in RESERVED:
        raise RuntimeError(
            f"{name} can't be a share view column that consumers write without quotes (Snowflake would need "
            f'"{upper}"): rename it in the public contract ({CONTRACT.name})'
        )
    return upper


def undescribed(columns: list[str], table: str, contract: dict) -> list[str]:
    """Columns the contract doesn't describe (none when it describes them all)."""
    described = contract.get(table, ("", {}))[1]
    return [c for c in columns if not described.get(c.lower())]


def _described(columns: list[str], table: str, contract: dict) -> list[tuple[str, str]]:
    """(column, its description) for each column; raises if the contract doesn't describe one."""
    if table not in contract or not contract[table][0]:
        raise RuntimeError(f"{table} has no description in the public contract ({CONTRACT.name})")
    if missing := undescribed(columns, table, contract):
        raise RuntimeError(
            f"{table} has columns the public contract doesn't describe: {', '.join(missing)}. Describe them in "
            f"{CONTRACT.name} (the listing's data dictionary comes from it)."
        )
    return [(c, contract[table][1][c.lower()]) for c in columns]


def _warn_absent(obj: str, columns: list[str], table: str, contract: dict) -> None:
    """A contract column the object doesn't have yet is skipped with a warning, never commented (that would fail
    the load): e.g. a share view that only a migration will rebuild with it."""
    have = {c.lower() for c in columns}
    if absent := [c for c in contract.get(table, ("", {}))[1] if c not in have]:
        print(
            f"warning: {obj} has no column {', '.join(absent)} from the public contract: not described",
            file=sys.stderr,
        )


def table_comments(table: str, columns: list[str], contract: dict) -> list[str]:
    """Comments for a loaded table and each of its columns (as Snowflake names them). Columns use ALTER TABLE ...
    ALTER COLUMN ... COMMENT with a schema.table name: COMMENT ON COLUMN with a qualified column name fails in
    Snowflake ("Object ... does not exist or not authorized")."""
    _warn_absent(f"{SCHEMA}.{table}", columns, table, contract)
    cols = _described(columns, table, contract)
    return [f"comment on table {SCHEMA}.{table} is {sql_string(contract[table][0])}"] + [
        f"alter table {SCHEMA}.{table} alter column {_ident(c)} comment {sql_string(d)}" for c, d in cols
    ]


def view_comments(table: str, columns: list[str], contract: dict) -> list[str]:
    """Comments for an existing share view and its columns (ALTER VIEW ... ALTER COLUMN ... COMMENT: Snowflake
    documents COMMENT ON COLUMN for table columns only)."""
    view = f"SHARE.{SHARE_VIEWS[table]}"
    _warn_absent(view, columns, table, contract)
    cols = _described(columns, table, contract)
    return [f"comment on view {view} is {sql_string(contract[table][0])}"] + [
        f"alter view {view} alter column {_ident(c)} comment {sql_string(d)}" for c, d in cols
    ]


def create_view(table: str, columns: list[str], contract: dict) -> str:
    """The share view over a table, with its descriptions in the definition itself, so recreating it can't drop
    them, and its columns renamed to upper case (`columns` are the table's names, selected quoted).
    Snowflake's order: column list, then COPY GRANTS, then COMMENT =, then AS."""
    cols = _described(columns, table, contract)
    listed = ", ".join(f"{view_column(c)} comment {sql_string(d)}" for c, d in cols)
    return (
        f"create or replace secure view SHARE.{SHARE_VIEWS[table]} ({listed}) copy grants "
        f"comment = {sql_string(contract[table][0])} as select {', '.join(_ident(c) for c, _ in cols)} "
        f"from {SCHEMA}.{table}"
    )


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


def _column_names(cur, schema: str, name: str) -> list[str]:
    """A table's or view's columns as Snowflake names them; [] if it doesn't exist."""
    return [
        r[0]
        for r in cur.execute(
            f"select column_name from information_schema.columns "
            f"where table_schema = '{schema}' and table_name = '{name.upper()}'"
        ).fetchall()
    ]


def _columns(cur, schema: str, name: str) -> list[str]:
    """A table's or view's columns, sorted and upper case; [] if it doesn't exist."""
    return sorted(c.upper() for c in _column_names(cur, schema, name))


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


def stale(cur, export_dir: Path) -> tuple[list[str], list[str], list[str]]:
    """Tables and share views whose columns differ from the export, by name in any case (the tables keep the
    file's lower case, the views are upper case). COPY ... match_by_column_name silently loads NULL into a column
    the file no longer has, and a secure view keeps the columns it was created with, so neither can be used as
    is after a public contract change. One that doesn't exist yet doesn't differ: the load creates the table, and
    admin_setup.sql part 2 the view.

    Third, the share views that have the export's columns but not all in upper case (made by admin_setup.sql
    part 2's `select *`, or before the views were upper case): any load recreates them, no migration needed."""
    import pyarrow.parquet as pq

    tables, views, recase = [], [], []
    for t in PUBLIC_TABLES:
        want = sorted(c.upper() for c in pq.read_schema(export_dir / f"{t}.parquet").names)
        if (have := _columns(cur, SCHEMA, t)) and have != want:
            tables.append(t)
        if names := _column_names(cur, "SHARE", SHARE_VIEWS[t]):
            if sorted(c.upper() for c in names) != want:
                views.append(t)
            elif any(c != c.upper() for c in names):
                recase.append(t)
    return tables, views, recase


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


class _DryRun:
    """A cursor that runs only reads (select, show) and records every other statement instead of running it, with
    what each read returned, so a dry run prints what a load would see and do and changes nothing."""

    READS = ("select ", "show ")

    def __init__(self, cur) -> None:
        self._cur = cur
        self.reads: list[dict] = []
        self.writes: list[str] = []
        self.description = None
        self._rows: list = []

    def execute(self, sql: str):
        if sql.lower().startswith(self.READS) and not sql.lower().startswith("select count(*)"):
            self._rows = self._cur.execute(sql).fetchall()
            self.description = self._cur.description
            self.reads.append({"sql": sql, "rows": [list(map(str, r)) for r in self._rows]})
        else:  # a write, or a row count of a table the load would have just reloaded
            self.writes.append(sql)
            self._rows, self.description = [(0,)], None
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self) -> None:
        self._cur.close()


def load(export_dir: Path, con=None, migrate: bool = False, dry_run: bool = False) -> dict:
    """Load one export. If any table's or share view's columns differ from the export (a new public contract
    version), nothing is loaded unless `migrate`. Then the role must own the share views; the files are uploaded
    first, the differing tables are dropped and loaded fresh, and every share view is recreated with `copy grants`,
    so the share never loses one. Each step can be repeated, so a migration that failed partway is finished by
    running it again. A share view with the export's columns but not all in upper case is recreated by any load,
    after the same ownership and share checks. `dry_run` changes nothing: it runs only the checks' reads and returns them (tables, share
    views, owners, grants), any refusal, and the statements the load would run."""
    manifest = json.loads((export_dir / "manifest.json").read_text())
    exported = re.sub(r"[^0-9T]", "", manifest["exported_at"].split("+")[0]) + "Z"  # 20261027T020148Z
    run_path = f"run={manifest['as_of_run']}/exported={exported}"
    share = os.environ.get("SNOWFLAKE_SHARE", "COFFEE_DATA_SHARE").upper()
    con = con or connect()
    cur = _DryRun(con.cursor()) if dry_run else con.cursor()
    try:
        if dry_run:
            try:
                _load(cur, export_dir, run_path, share, migrate)
                refused = None
            except RuntimeError as e:
                refused = str(e)
            return {"dry_run": True, "run": manifest["as_of_run"], "migrate": migrate, "refused": refused,
                    "reads": cur.reads, "statements": cur.writes}  # fmt: skip
        counts = _load(cur, export_dir, run_path, share, migrate)
    finally:
        cur.close()
        con.close()
    mismatched = {
        t: (counts[t], manifest["tables"][t]) for t in PUBLIC_TABLES if counts[t] != manifest["tables"][t]
    }
    if mismatched:
        raise RuntimeError(f"row counts differ from the export (snowflake, export): {mismatched}")
    return {"run": manifest["as_of_run"], "tables": counts}


def _load(cur, export_dir: Path, run_path: str, share: str, migrate: bool) -> dict:
    """The checks, then the load; returns the row count of each table."""
    import pyarrow.parquet as pq

    contract = contract_descriptions()
    order = {t: pq.read_schema(export_dir / f"{t}.parquet").names for t in PUBLIC_TABLES}
    for t in PUBLIC_TABLES:  # every column gets a description and an unquoted view name, or nothing is loaded
        _described(order[t], t, contract)
        for c in order[t]:
            view_column(c)

    cur.execute(f"create schema if not exists {SCHEMA}")
    cur.execute(f"create file format if not exists {SCHEMA}.PARQUET_FF type = parquet")
    cur.execute(
        f"create stage if not exists {SCHEMA}.{STAGE} file_format = {SCHEMA}.PARQUET_FF "
        "comment = 'Weekly facts-only exports, one folder per run'"
    )
    # Everything is checked before anything changes, so a mismatch never leaves Snowflake half on the new export.
    tables, views, recase = stale(cur, export_dir)
    if (tables or views) and not migrate:
        raise RuntimeError(
            f"{', '.join(dict.fromkeys(tables + views))} in Snowflake have different columns from the export: "
            "the public contract changed. Nothing was loaded. Run `coffeedata load-snowflake --migrate` (or "
            "the weekly workflow with only `snowflake_migrate` set) to recreate those tables and the share views."
        )
    # The views to recreate: a migration recreates every existing one, any load those not yet in upper case.
    present = [
        t for t in PUBLIC_TABLES if t in recase or (migrate and _columns(cur, "SHARE", SHARE_VIEWS[t]))
    ]
    if present:  # checked first, so a migration that can't finish changes nothing
        role = str(cur.execute("select current_role()").fetchone()[0]).upper()
        grants = {}
        for t in present:
            view = f"SHARE.{SHARE_VIEWS[t]}"
            try:
                grants[t] = _view_grants(cur, view)
            except Exception as e:  # noqa: BLE001 - snowflake.connector's ProgrammingError
                if "privilege" not in str(e).lower() and "not authorized" not in str(e).lower():
                    raise
                grants[t] = (
                    "another role",
                    set(),
                )  # only a role with rights on the view can list its grants
        for t, (owner, _) in grants.items():
            if owner != role:
                raise RuntimeError(
                    f"SHARE.{SHARE_VIEWS[t]} is owned by {owner}, not {role}, so {role} can't recreate it. "
                    f"Nothing was changed. Its owner (or ACCOUNTADMIN) can give it back, keeping its grants: "
                    f"grant ownership on view SHARE.{SHARE_VIEWS[t]} to role {role} copy current grants"
                )
        # `copy grants` keeps each view's grant to the share, so only a view the share is missing (and the
        # share exists: another view is in it) is granted, before anything else changes. That grant needs the
        # share's owner, which is COFFEE_LOADER under admin_setup.sql (a share's ownership can't be moved).
        granted: list[str] = []
        if any(share in shares for _, shares in grants.values()):
            for t in (t for t, (_, shares) in grants.items() if share not in shares):
                try:
                    cur.execute(f"grant select on view SHARE.{SHARE_VIEWS[t]} to share {share}")
                    granted.append(f"SHARE.{SHARE_VIEWS[t]}")
                except Exception as e:  # noqa: BLE001 - snowflake.connector's ProgrammingError
                    kept = f" (granted to it so far: {', '.join(granted)})" if granted else ""
                    raise RuntimeError(
                        f"SHARE.{SHARE_VIEWS[t]} is missing from share {share}, and {role} can't grant it "
                        f"({e}): that needs the share's owner. No table or view was changed{kept}. Grant it "
                        f"as the share's owner: grant select on view SHARE.{SHARE_VIEWS[t]} to share {share}"
                    ) from e
    for table in PUBLIC_TABLES:  # uploaded before anything is dropped
        f = (export_dir / f"{table}.parquet").resolve()
        cur.execute(f"put 'file://{f}' @{SCHEMA}.{STAGE}/{run_path}/ auto_compress = false overwrite = false")
    for table in tables:
        cur.execute(f"drop table if exists {SCHEMA}.{table}")
    counts = {}
    for table in PUBLIC_TABLES:
        for sql in statements(table, run_path):
            cur.execute(sql)
        counts[table] = cur.execute(f"select count(*) from {SCHEMA}.{table}").fetchone()[0]

    def as_exported(names: list[str], table: str) -> list[str]:
        """Snowflake's column names, in the export's column order (information_schema returns them in any order).
        Only a dry run, which created and dropped nothing, sees other columns; it shows the export's."""
        position = {c.lower(): n for n, c in enumerate(order[table])}
        if sorted(c.lower() for c in names) != sorted(position):
            return order[table]
        return sorted(names, key=lambda c: position[c.lower()])

    # The descriptions, from the contract, on every load: idempotent, and the listing's data dictionary.
    for table in PUBLIC_TABLES:
        for sql in table_comments(table, as_exported(_column_names(cur, SCHEMA, table), table), contract):
            cur.execute(sql)
    # A migration rebuilds every existing view, not only the changed ones: one left over from a failed migration
    # is rebuilt too. Views that don't exist yet are left to admin_setup.sql part 2; the next load recreates them
    # with upper-case columns and their descriptions.
    for table in present:
        cur.execute(create_view(table, as_exported(_column_names(cur, SCHEMA, table), table), contract))
    for table in PUBLIC_TABLES:  # the views this load didn't just recreate with their descriptions
        if table not in present and (names := _column_names(cur, "SHARE", SHARE_VIEWS[table])):
            for sql in view_comments(table, names, contract):  # only the columns it has
                cur.execute(sql)

    return counts
