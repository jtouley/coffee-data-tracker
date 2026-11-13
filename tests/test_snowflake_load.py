import json
import re

import pytest
import yaml

from coffeedata import snowflake_load
from coffeedata.public import PUBLIC_TABLES

REAL_CONTRACT = snowflake_load.CONTRACT


def write_contract(path, columns=("coffee_id", "variant_id"), describe=lambda t, c: f"{t} {c}."):
    """A contract for the test exports: every table with these columns."""
    schema = [
        {"name": t, "description": f"The {t} table.",
         "properties": [{"name": c, "description": describe(t, c)} for c in columns]}
        for t in PUBLIC_TABLES
    ]  # fmt: skip
    path.write_text(yaml.safe_dump({"schema": schema}))
    return path


@pytest.fixture(autouse=True)
def test_contract(tmp_path_factory, monkeypatch):
    """The loader reads the contract at load time; these tests' exports have their own columns."""
    path = write_contract(tmp_path_factory.mktemp("contract") / "public-catalog.odcs.yaml")
    monkeypatch.setattr(snowflake_load, "CONTRACT", path)
    return path


class FakeCursor:
    def __init__(self, counts, columns=None):
        self.sql, self.counts, self._last, self.columns = [], counts, None, dict(columns or {})

    def execute(self, sql):
        self.sql.append(sql)
        self._last = sql
        if m := re.match(r"create table if not exists MARKET\.(\w+)", sql):
            self.columns.setdefault(m[1], ["coffee_id", "variant_id"])  # infer_schema keeps the file's case
        return self

    def fetchone(self):
        table = self._last.rsplit(".", 1)[-1]
        return (self.counts[table],)

    def fetchall(self):  # information_schema.columns: the table's existing columns (none: not created yet)
        table = self._last.rsplit("table_name = '", 1)[-1].rstrip("'").lower()
        return [(c,) for c in self.columns.get(table, [])]

    def close(self):
        pass


class FakeConn:
    def __init__(self, counts, columns=None):
        self.cur = FakeCursor(counts, columns)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def export_dir(tmp_path, counts, exported_at="2026-10-27T02:01:48+00:00"):
    import pyarrow as pa
    import pyarrow.parquet as pq

    for t in PUBLIC_TABLES:
        pq.write_table(pa.table({"coffee_id": ["x"], "variant_id": ["v"]}), tmp_path / f"{t}.parquet")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"as_of_run": "2026-10-26", "exported_at": exported_at, "tables": counts})
    )
    return tmp_path


def test_reexporting_a_week_loads_the_new_files(tmp_path):
    """A second export of the same week goes to its own folder, so COPY can't pick up the first one's files."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    paths = []
    for when in ("2026-10-27T02:01:48+00:00", "2026-10-27T09:30:00+00:00"):
        con = FakeConn(counts)
        snowflake_load.load(export_dir(tmp_path, counts, when), con)
        copy = next(s for s in con.cur.sql if s.startswith("copy into MARKET.public_coffee"))
        paths.append(copy.split(" from ")[1].split(" ")[0])
    assert paths == [
        "@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=20261027T020148Z/public_coffee.parquet",
        "@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=20261027T093000Z/public_coffee.parquet",
    ]


def test_load_stages_per_run_and_reloads_each_table(tmp_path):
    counts = {t: i + 1 for i, t in enumerate(PUBLIC_TABLES)}
    con = FakeConn(counts)
    out = snowflake_load.load(export_dir(tmp_path, counts), con)
    assert out == {"run": "2026-10-26", "tables": counts}
    sql = con.cur.sql
    puts = [s for s in sql if s.startswith("put ")]
    assert len(puts) == 4 and all(
        "@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=" in p and "overwrite = false" in p for p in puts
    )
    for t in PUBLIC_TABLES:  # truncate + copy inside one transaction per table
        i = sql.index(f"truncate table MARKET.{t}")
        assert (
            sql[i - 1] == "begin"
            and sql[i + 1].startswith(f"copy into MARKET.{t}")
            and sql[i + 2] == "commit"
        )


def test_row_count_mismatch_fails(tmp_path):
    counts = {t: 5 for t in PUBLIC_TABLES}
    con = FakeConn(counts | {"public_coffee": 4})
    with pytest.raises(RuntimeError, match="row counts differ"):
        snowflake_load.load(export_dir(tmp_path, counts), con)


def test_private_key_pem_to_der():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    import base64

    expected = snowflake_load.private_key_der(pem)
    assert serialization.load_der_private_key(expected, password=None).key_size == 2048
    one_line = base64.b64encode(pem.encode()).decode()  # base64 -w0 coffee_rsa.p8
    escaped = pem.replace("\n", "\\n")  # pasted with literal \n
    for variant in (one_line, escaped, "  " + pem + "\n"):
        assert snowflake_load.private_key_der(variant) == expected


def test_missing_credentials_is_a_clear_error(monkeypatch):
    for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit, match="SNOWFLAKE_ACCOUNT"):
        snowflake_load.connect()


class FakeSnowflake:
    """Enough of Snowflake to follow a migration: tables and views with their columns, who owns the views and the
    share, and which views are in the share. Column names are case sensitive as in Snowflake: a table made by
    infer_schema has the file's lower-case names, and a view's column list names its columns (unquoted: upper
    case). `fail_on` makes the first statement containing it raise, as a failure
    partway through would. There is no `show shares`: without IMPORT SHARE (which admin_setup.sql doesn't grant)
    it lists nothing."""

    def __init__(
        self, counts, tables=None, views=None, owner="COFFEE_LOADER", role="COFFEE_LOADER", shared=None
    ):
        self.counts, self.tables, self.views = counts, dict(tables or {}), dict(views or {})
        self.owner, self.role = owner, role  # owner: the share's owning role, None if there is no share
        self.grants = set(self.views if shared is None else shared) if owner else set()
        self.view_owner = {v: "COFFEE_LOADER" for v in self.views}
        self.sql, self.fail_on, self.description, self._rows = [], None, None, []
        self.export_cols: list[str] | dict[str, list[str]] = ["coffee_id", "variant_id"]  # or by table
        self.comments: dict[str, str] = {}  # "MARKET.t", "MARKET.t.COL", "SHARE.v", "SHARE.v.COL" -> literal

    def cursor(self):
        return self

    def close(self):
        pass

    def _comment(self, key, literal, cols=None, col=None):
        if cols is not None and col not in cols:
            raise RuntimeError(f"invalid identifier {col}")  # quoted names are case sensitive
        assert re.fullmatch(r"'(?:[^'\\]|''|\\\\)*'", literal), literal  # one closed, escaped literal
        self.comments[key + (f".{col}" if col else "")] = literal

    def execute(self, sql):
        self.sql.append(sql)
        if self.fail_on and self.fail_on in sql:
            self.fail_on = None
            raise RuntimeError(f"failed: {sql}")
        self.description, self._rows = None, []
        if m := re.search(r"table_schema = '(\w+)' and table_name = '(\w+)'", sql):
            have = (self.tables if m[1] == "MARKET" else self.views).get(m[2].lower(), [])
            self._rows = [(c,) for c in have]
        elif sql.startswith("show shares"):
            raise AssertionError("show shares lists nothing without IMPORT SHARE")
        elif (m := re.match(r"show grants on view SHARE\.(\w+)", sql)) and self.view_owner[m[1]] != self.role:
            raise RuntimeError("SQL access control error: Insufficient privileges to operate on view")
        elif m := re.match(r"show grants on view SHARE\.(\w+)", sql):
            self.description = [("created_on",), ("privilege",), ("granted_on",), ("name",), ("granted_to",),
                                ("grantee_name",)]  # fmt: skip
            self._rows = [("t", "OWNERSHIP", "VIEW", m[1], "ROLE", self.view_owner[m[1]])]
            if m[1] in self.grants:
                self._rows.append(("t", "SELECT", "VIEW", m[1], "SHARE", "ORG.ACCT.COFFEE_DATA_SHARE"))
        elif sql == "select current_role()":
            self._rows = [(self.role,)]
        elif m := re.match(r"drop table if exists MARKET\.(\w+)", sql):
            self.tables.pop(m[1], None)
        elif m := re.match(r"create table if not exists MARKET\.(\w+)", sql):
            cols = self.export_cols
            self.tables.setdefault(m[1], cols[m[1]] if isinstance(cols, dict) else cols)
        elif sql.startswith("comment on column"):
            raise RuntimeError("Object 'COFFEE_ID' does not exist or not authorized")  # as Snowflake answers
        elif m := re.fullmatch(r"comment on table MARKET\.(\w+) is ('.*')", sql):
            self._comment(f"MARKET.{m[1]}", m[2])
        elif m := re.fullmatch(r"alter table MARKET\.(\w+) alter column \"(\w+)\" comment ('.*')", sql):
            self._comment(f"MARKET.{m[1]}", m[3], self.tables[m[1]], m[2])
        elif m := re.fullmatch(r"comment on view SHARE\.(\w+) is ('.*')", sql):
            self._comment(f"SHARE.{m[1]}", m[2])
        elif m := re.fullmatch(r"alter view SHARE\.(\w+) alter column \"(\w+)\" comment ('.*')", sql):
            self._comment(f"SHARE.{m[1]}", m[3], self.views[m[1]], m[2])
        elif m := re.match(r"select count\(\*\) from MARKET\.(\w+)", sql):
            self._rows = [(self.counts[m[1]],)]
        elif m := re.fullmatch(
            r"create or replace secure view SHARE\.(\w+) \((.*)\) (copy grants )?comment = ('(?:[^']|'')*') "
            r"as select ([\w\", ]+) from MARKET\.(\w+)",
            sql,
        ):  # Snowflake's order: column list, copy grants, comment, as
            if self.view_owner.setdefault(m[1], self.role) != self.role:
                raise RuntimeError("insufficient privileges to operate on view")
            listed = [
                (c.strip('"') if c.startswith('"') else c.upper(), literal)  # unquoted: upper case
                for c, literal in re.findall(r"(\"\w+\"|\w+) comment ('(?:[^']|'')*')(?:, |$)", m[2])
            ]
            selected = re.findall(r'"(\w+)"', m[5])  # quoted: as the table names them, or Snowflake refuses
            assert len(listed) == len(selected) and sorted(selected) == sorted(self.tables[m[6]])
            self.views[m[1]] = [c for c, _ in listed]
            self._comment(f"SHARE.{m[1]}", m[4])
            for col, literal in listed:
                self._comment(f"SHARE.{m[1]}", literal, self.views[m[1]], col)
            if not m[3]:
                self.grants.discard(m[1])  # create or replace without copy grants drops the view's grants
        elif m := re.match(r"grant select on view SHARE\.(\w+) to share", sql):
            if self.role != self.owner:
                raise RuntimeError("insufficient privileges to operate on share")
            self.grants.add(m[1])
        return self

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


OLD = ["coffee_id", "bag_id"]  # loaded before the rename (infer_schema keeps the file's lower case)
VIEWS = {"coffee": OLD, "price_event": OLD, "market_weekly": OLD, "roaster": OLD}  # select *: lower case too
NEW = ["coffee_id", "variant_id"]
NEW_VIEW = ["COFFEE_ID", "VARIANT_ID"]  # what consumers see: upper case, no quotes needed


def old_snowflake(counts, **kw):
    return FakeSnowflake(counts, tables={t: OLD for t in PUBLIC_TABLES}, views=VIEWS, **kw)


def healthy(sf):
    return (
        all(sf.tables.get(t) == NEW for t in PUBLIC_TABLES)
        and all(sf.views.get(v) == NEW_VIEW for v in VIEWS)
        and sf.grants == set(VIEWS)
    )


def unchanged(sf):
    return not any(s.startswith(("put ", "drop", "truncate", "create or replace")) for s in sf.sql) and all(
        sf.tables[t] == OLD for t in PUBLIC_TABLES
    )


def test_a_changed_public_contract_loads_nothing_until_migrated(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = FakeSnowflake(counts, tables={"public_price_event": OLD})  # the others are new tables
    with pytest.raises(RuntimeError, match="contract changed. Nothing was loaded"):
        snowflake_load.load(export_dir(tmp_path, counts), sf)
    # Everything is checked first: nothing was uploaded, dropped, truncated or copied.
    assert not any(s.startswith(("put ", "drop", "truncate", "copy into")) for s in sf.sql)


def test_migrate_uploads_before_dropping_and_rebuilds_every_view_keeping_its_grants(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts)
    assert snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)["tables"] == counts
    assert healthy(sf)
    sql = sf.sql
    first_put = min(n for n, s in enumerate(sql) if s.startswith("put "))
    checks = [n for n, s in enumerate(sql) if s.startswith("show grants on view")]
    last_check = max(n for n in checks if n < first_put)
    last_put = max(n for n, s in enumerate(sql) if s.startswith("put "))
    first_drop = min(n for n, s in enumerate(sql) if s.startswith("drop table"))
    assert last_check < last_put < first_drop  # grants checked, files up, then drops
    last_view = max(n for n, s in enumerate(sql) if s.startswith("create or replace secure view"))
    assert (
        len([n for n in checks if n > last_view]) == 4
    )  # and each view read back in the share once recreated
    views = [s for s in sql if s.startswith("create or replace secure view")]
    assert len(views) == 4 and all(re.search(r"\) copy grants comment = '.*' as select ", v) for v in views)
    assert not any(
        s.startswith("grant") for s in sql
    )  # copy grants kept them: nothing needed the share's owner


@pytest.mark.parametrize(
    "fail_on",
    ["put 'file://", "drop table if exists MARKET.public_roaster", "copy into MARKET.public_price_event",
     "create or replace secure view SHARE.market_weekly"],
)  # fmt: skip
def test_a_migration_that_failed_partway_is_finished_by_running_it_again(tmp_path, fail_on):
    counts = {t: 1 for t in PUBLIC_TABLES}
    export = export_dir(tmp_path, counts)
    sf = old_snowflake(counts)
    sf.fail_on = fail_on
    with pytest.raises(RuntimeError, match="failed"):
        snowflake_load.load(export, sf, migrate=True)
    assert sf.grants == set(VIEWS)  # copy grants: no view left the share, even mid-way
    if not healthy(sf):  # an ordinary weekly load refuses a half-migrated Snowflake rather than loading NULLs
        with pytest.raises(RuntimeError, match="contract changed"):
            snowflake_load.load(export, sf)
    snowflake_load.load(export, sf, migrate=True)
    assert healthy(sf)
    snowflake_load.load(export, sf)  # and the weekly load works again
    assert healthy(sf)


def test_migrate_works_when_another_role_owns_the_share(tmp_path):
    """Not the documented setup (admin_setup.sql creates the share as COFFEE_LOADER, and a share's ownership can't be
    moved; the PR's owner step checks it), but a migration doesn't need the share's owner: every view is already in
    it, and copy grants keeps it there (as this fake models Snowflake; not checked against a live account)."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts, owner="ACCOUNTADMIN")
    snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert healthy(sf)


def test_migrate_stops_before_changing_anything_if_another_role_owns_a_view(tmp_path):
    """E.g. after a migration run as ACCOUNTADMIN, which then owns the views it recreated."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts)
    sf.view_owner["roaster"] = "ACCOUNTADMIN"  # COFFEE_LOADER then can't even list the view's grants
    with pytest.raises(RuntimeError, match="SHARE.roaster is owned by another role, not COFFEE_LOADER.*Nothing was"
                                           ".*grant ownership on view SHARE.roaster to role COFFEE_LOADER copy"):  # fmt: skip
        snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert unchanged(sf)


def test_a_view_missing_from_the_share_is_granted_first(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts, shared={"coffee", "price_event", "market_weekly"})
    snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert healthy(sf)
    grant = sf.sql.index("grant select on view SHARE.roaster to share COFFEE_DATA_SHARE")
    assert grant < min(n for n, s in enumerate(sf.sql) if s.startswith("put "))

    # Granting it needs the share's owner: without that, the migration stops before changing anything.
    sf = old_snowflake(counts, owner="ACCOUNTADMIN", shared={"coffee", "price_event", "market_weekly"})
    with pytest.raises(
        RuntimeError, match="SHARE.roaster is missing from share COFFEE_DATA_SHARE.*No table or view was"
    ):
        snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert unchanged(sf)


def test_migrate_before_the_share_and_its_views_exist_only_reloads_the_tables(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = FakeSnowflake(counts, tables={t: OLD for t in PUBLIC_TABLES}, owner=None)
    snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert all(sf.tables[t] == NEW for t in PUBLIC_TABLES) and sf.views == {}
    assert not any(s.startswith(("grant", "create or replace secure view", "show")) for s in sf.sql)


def test_same_columns_load_as_usual(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    con = FakeConn(counts, columns={"public_coffee": ["coffee_id", "variant_id"], "coffee": NEW_VIEW})
    assert snowflake_load.load(export_dir(tmp_path, counts), con)["tables"] == counts
    assert not any(s.startswith(("drop table", "create or replace secure view", "show")) for s in con.cur.sql)


def test_dry_run_changes_nothing_and_shows_the_plan(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts)
    out = snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True, dry_run=True)
    writes = ("create", "put ", "drop", "truncate", "copy into", "grant", "begin", "commit")
    assert not any(s.lower().startswith(writes) for s in sf.sql)  # only reads reached Snowflake
    assert sf.tables["public_coffee"] == OLD and sf.views["coffee"] == OLD
    assert out["dry_run"] and out["refused"] is None
    assert any(s.startswith("drop table if exists MARKET.public_coffee") for s in out["statements"])
    assert any(s.startswith("create or replace secure view SHARE.coffee") for s in out["statements"])
    assert any(
        "show grants on view SHARE.coffee" in r["sql"] for r in out["reads"]
    )  # owners and grants shown

    sf = old_snowflake(counts)
    refused = snowflake_load.load(export_dir(tmp_path, counts), sf, dry_run=True)  # without --migrate
    assert "contract changed" in refused["refused"] and not any(s.lower().startswith(writes) for s in sf.sql)


def contract_export(tmp_path, counts):
    """An export with the real public contract's columns, as the loader would get it."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    contract = snowflake_load.contract_descriptions(REAL_CONTRACT)
    for t in PUBLIC_TABLES:
        pq.write_table(pa.table({c: [None] for c in contract[t][1]}), tmp_path / f"{t}.parquet")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"as_of_run": "2026-10-26", "exported_at": "2026-10-27T02:01:48+00:00", "tables": counts})
    )
    return tmp_path, contract


@pytest.mark.parametrize("migrate", [False, True])
def test_every_contract_column_is_described_on_its_table_and_view(tmp_path, monkeypatch, migrate):
    """The real contract, read at load time: each table, view and column gets its description on every load
    (existing views by ALTER VIEW), and a migration writes the view's into its definition, so a recreate can't drop
    them."""
    monkeypatch.setattr(snowflake_load, "CONTRACT", REAL_CONTRACT)
    counts = {t: 1 for t in PUBLIC_TABLES}
    export, contract = contract_export(tmp_path, counts)
    names = {t: list(contract[t][1]) for t in PUBLIC_TABLES}  # as infer_schema names them: the file's case
    old = {t: names[t][:-1] for t in PUBLIC_TABLES}  # e.g. roaster without release_run_date, to migrate
    tables = old if migrate else names
    upper = {t: [c.upper() for c in names[t]] for t in PUBLIC_TABLES}  # views already upper case
    views = {v: (old if migrate else upper)[t] for t, v in snowflake_load.SHARE_VIEWS.items()}
    sf = FakeSnowflake(counts, tables=tables, views=views)
    sf.export_cols = names
    snowflake_load.load(export, sf, migrate=migrate)
    want = {}
    for t, (desc, cols) in contract.items():
        if t not in PUBLIC_TABLES:
            continue
        for obj, case in ((f"MARKET.{t}", str.lower), (f"SHARE.{snowflake_load.SHARE_VIEWS[t]}", str.upper)):
            want[obj] = snowflake_load.sql_string(desc)
            want |= {f"{obj}.{case(c)}": snowflake_load.sql_string(d) for c, d in cols.items()}
    assert sf.comments == want and all(len(v) > 2 for v in want.values())
    recreated = [s for s in sf.sql if s.startswith("create or replace secure view")]
    altered = [s for s in sf.sql if s.startswith("alter view")]
    assert (len(recreated), bool(altered)) == ((4, False) if migrate else (0, True))
    # A migration describes everything after the tables are dropped and reloaded: the dictionary is never empty.
    last_copy = max(n for n, s in enumerate(sf.sql) if s.startswith(("copy into", "drop table")))
    assert last_copy < min(
        n for n, s in enumerate(sf.sql) if s.startswith(("comment on", "alter", "create or r"))
    )


def test_no_statement_uses_comment_on_column(tmp_path):
    """Snowflake refuses `comment on column db.schema.table.col` ("Object ... does not exist or not
    authorized"): columns are commented with ALTER TABLE / ALTER VIEW ... ALTER COLUMN, under 2-part names."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    for migrate in (False, True):
        sf = old_snowflake(counts) if migrate else FakeSnowflake(counts, tables={}, views={})
        snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=migrate)
        assert not any(s.lower().startswith("comment on column") for s in sf.sql)
        commented = [s for s in sf.sql if s.startswith(("comment on", "alter table", "alter view"))]
        assert commented and all(
            re.match(r"(comment on \w+|alter \w+) (MARKET|SHARE)\.\w+ (is|alter column) ", s)
            for s in commented
        )


def test_descriptions_are_one_line_with_quotes_escaped(tmp_path, test_contract):
    write_contract(test_contract, describe=lambda t, c: "The roaster's own\n   id, as \\'listed\\'.")
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts)
    snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    literal = r"'The roaster''s own id, as \\''listed\\''.'"
    assert sf.comments["MARKET.public_coffee.coffee_id"] == literal
    assert sf.comments["SHARE.coffee.VARIANT_ID"] == literal
    assert snowflake_load.sql_string("it's\t a  'x'") == "'it''s a ''x'''"


def test_a_column_missing_from_the_contract_fails_before_anything_is_loaded(tmp_path, test_contract):
    """Fails loudly rather than skipping: an undescribed column would leave a gap in the listing's dictionary."""
    write_contract(test_contract, columns=("coffee_id",))
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = FakeSnowflake(counts)
    with pytest.raises(RuntimeError, match="public_coffee has columns the public contract doesn't describe: "
                                           "variant_id"):  # fmt: skip
        snowflake_load.load(export_dir(tmp_path, counts), sf)
    assert not any(
        s.startswith(("put ", "create", "drop", "truncate", "copy into", "comment", "alter")) for s in sf.sql
    )


def test_the_loader_reads_the_repository_contract():
    contract = snowflake_load.contract_descriptions(REAL_CONTRACT)
    assert set(PUBLIC_TABLES) <= set(contract) and all(d and cols for d, cols in contract.values())


def test_a_contract_column_snowflake_lacks_is_skipped_with_a_warning(tmp_path, test_contract, capsys):
    """Comments touch only columns Snowflake has; a contract column it lacks is reported, and the load goes on."""
    write_contract(test_contract, columns=("coffee_id", "variant_id", "release_run_date"))
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = FakeSnowflake(counts, tables={t: NEW for t in PUBLIC_TABLES}, views={v: NEW_VIEW for v in VIEWS})
    assert snowflake_load.load(export_dir(tmp_path, counts), sf)["tables"] == counts
    assert not any("RELEASE_RUN_DATE" in k for k in sf.comments) and "SHARE.roaster.VARIANT_ID" in sf.comments
    assert "warning: SHARE.roaster has no column release_run_date" in capsys.readouterr().err


def test_the_share_views_name_their_columns_in_upper_case_and_select_the_tables_quoted():
    contract = {"public_coffee": ("Coffees.", {"release_run_date": "The run.", "coffee_id": "The id."})}
    assert snowflake_load.create_view("public_coffee", ["release_run_date", "coffee_id"], contract) == (
        "create or replace secure view SHARE.coffee (RELEASE_RUN_DATE comment 'The run.', COFFEE_ID comment "
        "'The id.') copy grants comment = 'Coffees.' as select \"release_run_date\", \"coffee_id\" "
        "from MARKET.public_coffee"
    )


@pytest.mark.parametrize("name", ["order", "current_date", "price-usd", "2nd_price", "prix_€"])
def test_a_column_consumers_would_have_to_quote_fails_before_anything_is_loaded(
    tmp_path, test_contract, name
):
    write_contract(test_contract, columns=("coffee_id", name))
    import pyarrow as pa
    import pyarrow.parquet as pq

    counts = {t: 1 for t in PUBLIC_TABLES}
    export = export_dir(tmp_path, counts)
    for t in PUBLIC_TABLES:
        pq.write_table(pa.table({"coffee_id": ["x"], name: ["v"]}), export / f"{t}.parquet")
    sf = FakeSnowflake(counts)
    with pytest.raises(RuntimeError, match="without quotes"):
        snowflake_load.load(export, sf)
    assert not any(s.startswith(("put ", "create", "drop", "truncate", "copy into")) for s in sf.sql)
    assert snowflake_load.view_column("release_run_date") == "RELEASE_RUN_DATE"


def lower_case_snowflake(counts, **kw):
    """Loaded and shared before the views were upper case: each view's columns are the table's, lower case."""
    return FakeSnowflake(counts, tables={t: NEW for t in PUBLIC_TABLES}, views={v: NEW for v in VIEWS}, **kw)


def test_stale_compares_column_names_in_any_case(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    export = export_dir(tmp_path, counts)
    upper = FakeSnowflake(counts, tables={t: NEW for t in PUBLIC_TABLES}, views={v: NEW_VIEW for v in VIEWS})
    assert snowflake_load.stale(upper, export) == ([], [], [])  # lower-case tables, upper-case views: current
    assert snowflake_load.stale(lower_case_snowflake(counts), export) == ([], [], list(PUBLIC_TABLES))
    assert snowflake_load.stale(old_snowflake(counts), export) == (
        list(PUBLIC_TABLES),
        list(PUBLIC_TABLES),
        [],
    )


def test_the_first_load_after_the_upper_case_change_recreates_the_views_then_loads_as_usual(tmp_path):
    """No migration needed: the views keep their columns (only their case changes), their grants and their
    descriptions. The next weekly load finds them current and only refreshes the descriptions."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    export = export_dir(tmp_path, counts)
    sf = lower_case_snowflake(counts)
    assert snowflake_load.load(export, sf)["tables"] == counts
    assert healthy(sf)
    views = [s for s in sf.sql if s.startswith("create or replace secure view")]
    assert len(views) == 4 and all(" copy grants " in v for v in views)
    assert not any(s.startswith(("drop table", "grant", "alter view")) for s in sf.sql)
    assert sf.comments["SHARE.coffee.VARIANT_ID"] == "'public_coffee variant_id.'"

    sf.sql.clear()
    snowflake_load.load(export, sf)  # and every load after it is an ordinary one
    assert healthy(sf)
    assert not any(s.startswith(("create or replace", "drop table", "show grants")) for s in sf.sql)
    assert any(s.startswith('alter view SHARE.coffee alter column "VARIANT_ID" comment ') for s in sf.sql)


def test_a_lower_case_view_another_role_owns_is_left_as_is_and_the_data_still_loads(tmp_path, capsys):
    """Re-casing is cosmetic, so a load (not a migration) never holds the week's data back for it: the view another
    role owns keeps its lower-case columns, untouched, with a warning naming the grant that fixes it."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    sf.view_owner["roaster"] = "ACCOUNTADMIN"
    assert snowflake_load.load(export_dir(tmp_path, counts), sf)["tables"] == counts
    assert all(sf.views[v] == NEW_VIEW for v in ("coffee", "price_event", "market_weekly"))
    assert sf.views["roaster"] == NEW and sf.grants == set(VIEWS)
    assert not any("SHARE.roaster" in s for s in sf.sql if not s.startswith(("show ", "select ")))
    err = capsys.readouterr().err
    assert "warning: SHARE.roaster is owned by another role, not COFFEE_LOADER" in err
    assert "grant ownership on view SHARE.roaster to role COFFEE_LOADER copy current grants" in err


def test_a_migration_still_stops_if_another_role_owns_a_lower_case_view(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    sf.view_owner["roaster"] = "ACCOUNTADMIN"
    with pytest.raises(RuntimeError, match="SHARE.roaster is owned by another role, not COFFEE_LOADER"):
        snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert not any(s.startswith(("put ", "drop", "truncate", "create or replace")) for s in sf.sql)
    assert sf.views["coffee"] == NEW


def test_dry_run_shows_the_views_a_load_would_recreate_in_upper_case(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    out = snowflake_load.load(export_dir(tmp_path, counts), sf, dry_run=True)
    assert out["refused"] is None and sf.views["coffee"] == NEW  # nothing changed
    create = next(s for s in out["statements"] if s.startswith("create or replace secure view SHARE.coffee"))
    assert "(COFFEE_ID comment " in create and 'as select "coffee_id", "variant_id" from' in create
    # the views it would recreate aren't also described column by column (their definition carries it)
    assert not any(s.startswith("alter view") for s in out["statements"])


def _real_columns() -> dict[str, list[tuple[str, str]]]:
    """Each public table's (column, physicalType), from the repository's contract (dbt enforces the same)."""
    schema = yaml.safe_load(REAL_CONTRACT.read_text())["schema"]
    return {o["name"]: [(p["name"], p["physicalType"]) for p in o["properties"]] for o in schema}


def test_every_public_column_has_an_unquoted_upper_case_view_name():
    """The real contract's columns, as the share views will name them: none needs quotes, none collides."""
    columns = _real_columns()
    assert set(PUBLIC_TABLES) <= set(columns)
    for t in PUBLIC_TABLES:
        names = [snowflake_load.view_column(c) for c, _ in columns[t]]
        assert len(set(names)) == len(names), t


def test_the_listing_sample_queries_use_only_public_columns():
    """marketplace/LISTING.md's Snowflake sample queries run (in DuckDB) against empty SHARE views with the
    contract's upper-case columns and types, so a renamed or dropped column breaks this test, not the listing."""
    import duckdb

    listing = (REAL_CONTRACT.parents[1] / "marketplace" / "LISTING.md").read_text()
    sql = re.search(r"\*\*Sample queries\*\*.*?```sql\n(.*?)```", listing, re.S)[1]
    con = duckdb.connect()
    con.execute("create schema SHARE")
    for t, cols in _real_columns().items():
        if t in snowflake_load.SHARE_VIEWS:
            typed = ", ".join(f"{snowflake_load.view_column(c)} {ty}" for c, ty in cols)
            con.execute(f"create table SHARE.{snowflake_load.SHARE_VIEWS[t]} ({typed})")
    queries = [q.strip() for q in sql.split(";") if q.strip()]
    assert len(queries) == 4
    for q in queries:
        con.execute(q).fetchall()
        assert '"' not in q  # no quoted names


# --- post-merge audit of #106 -------------------------------------------------------------------------------------


def test_window_is_reserved_so_a_window_column_fails_before_anything_is_loaded():
    """WINDOW is on Snowflake's reserved-keyword list ("Reserved by ANSI") and was missing: a `window` column would
    have passed the pre-flight, reloaded the tables and then failed at `create view`."""
    assert "WINDOW" in snowflake_load.RESERVED
    with pytest.raises(RuntimeError, match="without quotes"):
        snowflake_load.view_column("window")


def test_a_name_is_checked_as_written_not_as_upper_cased():
    with pytest.raises(RuntimeError, match="without quotes"):
        snowflake_load.view_column("straße")  # "STRASSE" once upper-cased, which would pass


def test_columns_that_differ_only_in_case_fail_before_anything_is_loaded(tmp_path, test_contract):
    write_contract(test_contract, columns=("coffee_id", "Coffee_ID"))
    import pyarrow as pa
    import pyarrow.parquet as pq

    counts = {t: 1 for t in PUBLIC_TABLES}
    export = export_dir(tmp_path, counts)
    for t in PUBLIC_TABLES:
        pq.write_table(pa.table({"coffee_id": ["x"], "Coffee_ID": ["y"]}), export / f"{t}.parquet")
    sf = FakeSnowflake(counts)
    with pytest.raises(RuntimeError, match="differ only in case"):
        snowflake_load.load(export, sf)
    assert not any(s.startswith(("put ", "create", "drop", "truncate", "copy into")) for s in sf.sql)


def test_a_re_case_the_share_grant_fails_for_is_left_and_the_week_still_loads(tmp_path, capsys, monkeypatch):
    """Urgent before the 2026-11-10 load: a lower-case view missing from the share, with a role that can't grant
    it, refused the whole ordinary load. Re-casing is cosmetic, so the load now leaves that view and warns (also
    as a GitHub annotation); a migration still stops."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts, owner="ACCOUNTADMIN", shared={"coffee", "price_event", "market_weekly"})
    assert snowflake_load.load(export_dir(tmp_path, counts), sf)["tables"] == counts
    assert all(sf.views[v] == NEW_VIEW for v in ("coffee", "price_event", "market_weekly"))
    assert sf.views["roaster"] == NEW  # left as it was
    out, err = capsys.readouterr()
    assert "warning: SHARE.roaster is missing from share COFFEE_DATA_SHARE" in err
    assert "::warning title=Snowflake load::SHARE.roaster is missing from share" in out

    sf = lower_case_snowflake(counts, owner="ACCOUNTADMIN", shared={"coffee", "price_event", "market_weekly"})
    with pytest.raises(RuntimeError, match="No table or view was changed"):
        snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)


def test_a_re_case_whose_grants_cant_be_read_is_left_and_the_week_still_loads(tmp_path, capsys):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    sf.fail_on = "show grants on view SHARE.roaster"  # a transient error, not a missing privilege
    assert snowflake_load.load(export_dir(tmp_path, counts), sf)["tables"] == counts
    assert sf.views["roaster"] == NEW and sf.views["coffee"] == NEW_VIEW
    assert "warning: SHARE.roaster couldn't be checked" in capsys.readouterr().err


def test_a_view_that_left_the_share_when_recreated_fails_the_job_after_the_load(tmp_path, monkeypatch):
    """`copy grants` is what keeps a view in the share; the load reads each one back and fails the job (the data
    is loaded) if one isn't there any more, instead of consumers finding out."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    real = snowflake_load.create_view
    monkeypatch.setattr(snowflake_load, "create_view",
                        lambda *a: real(*a).replace(" copy grants ", " ") if a[0] == "public_roaster" else real(*a))  # fmt: skip
    with pytest.raises(RuntimeError, match="SHARE.roaster left share COFFEE_DATA_SHARE when recreated"):
        snowflake_load.load(export_dir(tmp_path, counts), sf)
    assert all(sf.tables[t] == NEW for t in PUBLIC_TABLES) and sf.views["coffee"] == NEW_VIEW


# Snowflake's "Reserved & limited keywords" page, the reserved table, as fetched on 2026-11-08
# (https://docs.snowflake.com/en/sql-reference/reserved-keywords). Re-fetch it when the pre-flight is next changed.
DOCUMENTED_RESERVED = """ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT CONNECTION CONSTRAINT
    CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DATABASE DELETE DISTINCT DROP ELSE
    EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT INTO IS
    ISSUE JOIN LATERAL LEFT LIKE LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER ORGANIZATION QUALIFY
    REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE TABLESAMPLE THEN TO TRIGGER TRUE
    TRY_CAST UNION UNIQUE UPDATE USING VALUES VIEW WHEN WHENEVER WHERE WINDOW WITH""".split()  # noqa: SIM905


def test_every_documented_reserved_keyword_is_refused():
    """The code may refuse more (ASOF and MATCH_CONDITION, from the ASOF JOIN syntax), never fewer."""
    assert not set(DOCUMENTED_RESERVED) - snowflake_load.RESERVED


def test_a_read_back_that_errors_still_describes_the_other_views_then_fails(tmp_path, capsys):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    real = sf.execute
    checks = {"n": 0}

    def flaky(sql):
        if sql == "show grants on view SHARE.coffee":
            checks["n"] += 1
            if checks["n"] == 2:  # the read-back, after the pre-flight's check
                raise RuntimeError("connection reset")
        return real(sql)

    sf.execute = flaky
    with pytest.raises(RuntimeError, match="SHARE.coffee couldn't be read back"):
        snowflake_load.load(export_dir(tmp_path, counts), sf)
    assert all(sf.views[v] == NEW_VIEW for v in VIEWS)  # recreated, data loaded
    assert "couldn't read back SHARE.coffee's grants (connection reset)" in capsys.readouterr().err


def test_a_dry_run_warns_on_stderr_but_adds_no_annotation(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = lower_case_snowflake(counts)
    sf.view_owner["roaster"] = "ACCOUNTADMIN"
    snowflake_load.load(export_dir(tmp_path, counts), sf, dry_run=True)
    out, err = capsys.readouterr()
    assert "warning: SHARE.roaster is owned by" in err and "::warning" not in out + err
    snowflake_load._warn("after")  # and the next real load annotates again
    assert "::warning title=Snowflake load::after" in capsys.readouterr().out
