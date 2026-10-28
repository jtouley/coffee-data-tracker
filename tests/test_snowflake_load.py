import json

import pytest

from coffeedata import snowflake_load
from coffeedata.public import PUBLIC_TABLES


class FakeCursor:
    def __init__(self, counts, columns=None):
        self.sql, self.counts, self._last, self.columns = [], counts, None, columns or {}

    def execute(self, sql):
        self.sql.append(sql)
        self._last = sql
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
    """Enough of Snowflake to follow a migration: tables and views with their columns, and the share's owner and
    grants. `fail_on` makes the first statement containing it raise, as a failure partway through would."""

    def __init__(self, counts, tables=None, views=None, owner="COFFEE_LOADER", role="COFFEE_LOADER"):
        self.counts, self.tables, self.views = counts, dict(tables or {}), dict(views or {})
        self.owner, self.role, self.grants = owner, role, set(self.views) if owner else set()
        self.sql, self.fail_on, self.description, self._rows = [], None, None, []
        self.export_cols = ["COFFEE_ID", "VARIANT_ID"]

    def cursor(self):
        return self

    def close(self):
        pass

    def execute(self, sql):
        import re

        self.sql.append(sql)
        if self.fail_on and self.fail_on in sql:
            self.fail_on = None
            raise RuntimeError(f"failed: {sql}")
        self.description, self._rows = None, []
        if m := re.search(r"table_schema = '(\w+)' and table_name = '(\w+)'", sql):
            have = (self.tables if m[1] == "MARKET" else self.views).get(m[2].lower(), [])
            self._rows = [(c,) for c in have]
        elif sql.startswith("show shares"):
            self.description = [("created_on",), ("kind",), ("name",), ("owner",)]
            self._rows = [("t", "OUTBOUND", "ORG.ACCT.COFFEE_DATA_SHARE", self.owner)] if self.owner else []
        elif sql == "select current_role()":
            self._rows = [(self.role,)]
        elif m := re.match(r"drop table if exists MARKET\.(\w+)", sql):
            self.tables.pop(m[1], None)
        elif m := re.match(r"create table if not exists MARKET\.(\w+)", sql):
            self.tables.setdefault(m[1], self.export_cols)
        elif m := re.match(r"select count\(\*\) from MARKET\.(\w+)", sql):
            self._rows = [(self.counts[m[1]],)]
        elif m := re.match(
            r"create or replace secure view SHARE\.(\w+) (copy grants )?as select \* from MARKET\.(\w+)", sql
        ):
            self.views[m[1]] = self.tables[m[3]]
            if not m[2]:
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


OLD = ["COFFEE_ID", "BAG_ID"]  # loaded before the rename
VIEWS = {"coffee": OLD, "price_event": OLD, "market_weekly": OLD, "roaster": OLD}


def old_snowflake(counts, **kw):
    return FakeSnowflake(counts, tables={t: OLD for t in PUBLIC_TABLES}, views=VIEWS, **kw)


def healthy(sf):
    new = ["COFFEE_ID", "VARIANT_ID"]
    return (
        all(sf.tables.get(t) == new for t in PUBLIC_TABLES)
        and all(sf.views.get(v) == new for v in VIEWS)
        and sf.grants == set(VIEWS)
    )


def test_a_changed_public_contract_loads_nothing_until_migrated(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = FakeSnowflake(counts, tables={"public_price_event": OLD})  # the others are new tables
    with pytest.raises(RuntimeError, match="contract changed. Nothing was loaded"):
        snowflake_load.load(export_dir(tmp_path, counts), sf)
    # Everything is checked first: nothing was uploaded, dropped, truncated or copied.
    assert not any(s.startswith(("put ", "drop", "truncate", "copy into")) for s in sf.sql)


def test_migrate_uploads_before_dropping_and_rebuilds_every_view_with_its_grants(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts)
    assert snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)["tables"] == counts
    assert healthy(sf)
    sql = sf.sql
    last_put = max(n for n, s in enumerate(sql) if s.startswith("put "))
    first_drop = min(n for n, s in enumerate(sql) if s.startswith("drop table"))
    assert sql.index("select current_role()") < last_put < first_drop  # owner checked, files up, then drops
    views = [s for s in sql if s.startswith("create or replace secure view")]
    assert len(views) == 4 and all(" copy grants as select * from MARKET." in v for v in views)


@pytest.mark.parametrize(
    "fail_on",
    ["put 'file://", "drop table if exists MARKET.public_roaster", "copy into MARKET.public_price_event",
     "create or replace secure view SHARE.market_weekly", "grant select on view SHARE.roaster"],
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


def test_migrate_stops_before_changing_anything_if_the_role_does_not_own_the_share(tmp_path):
    """After part 3 of admin_setup.sql the share may belong to ACCOUNTADMIN; the loader then can't grant to it."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = old_snowflake(counts, owner="ACCOUNTADMIN")
    with pytest.raises(RuntimeError, match="owned by ACCOUNTADMIN, not COFFEE_LOADER.*Nothing was changed"):
        snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert not any(s.startswith(("put ", "drop", "truncate", "create or replace")) for s in sf.sql)
    assert sf.tables["public_coffee"] == OLD and sf.views["coffee"] == OLD


def test_migrate_before_the_share_exists_rebuilds_only_existing_views(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    sf = FakeSnowflake(counts, tables={t: OLD for t in PUBLIC_TABLES}, views={"coffee": OLD}, owner=None)
    snowflake_load.load(export_dir(tmp_path, counts), sf, migrate=True)
    assert set(sf.views) == {"coffee"} and sf.views["coffee"] == ["COFFEE_ID", "VARIANT_ID"]
    assert not any(s.startswith("grant") for s in sf.sql)


def test_same_columns_load_as_usual(tmp_path):
    counts = {t: 1 for t in PUBLIC_TABLES}
    con = FakeConn(counts, columns={"public_coffee": ["COFFEE_ID", "VARIANT_ID"]})
    assert snowflake_load.load(export_dir(tmp_path, counts), con)["tables"] == counts
    assert not any(s.startswith(("drop table", "create or replace secure view", "show")) for s in con.cur.sql)
