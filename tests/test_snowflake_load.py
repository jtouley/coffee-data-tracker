import json
import re

import pytest

from coffeedata import snowflake_load
from coffeedata.public import PUBLIC_TABLES

LOADER = "COFFEE_LOADER"
OLD = ["BAG_ID", "COFFEE_ID"]  # loaded before the rename
NEW = ["COFFEE_ID", "VARIANT_ID"]
VIEWS = ["coffee", "price_event", "market_weekly", "roaster"]
NO_ACCESS = "SQL compilation error: Object does not exist or not authorized."


class FakeSnowflake:
    """Enough of Snowflake to follow a load and a migration. Every table and view has columns and an owning role,
    and information_schema lists only what the current role owns or was granted (`visible`). The share has an
    owner and the views granted to it, and only its owner can `show grants to share`. `fail_on` makes the first
    statement containing it raise, as a failure partway through would."""

    def __init__(self, counts, tables=None, views=None, share=True, shared=None, role=LOADER):
        self.counts, self.role = counts, role
        self.tables = {t: list(c) for t, c in (tables or {}).items()}
        self.views = {v: list(c) for v, c in (views or {}).items()}
        self.owner = {("MARKET", t): LOADER for t in self.tables} | {("SHARE", v): LOADER for v in self.views}
        self.visible: set[tuple[str, str]] = set()  # objects another role owns that this role was granted
        self.share_owner = LOADER if share else None
        self.grants = set(self.views if shared is None else shared) if share else set()
        self.sql, self.fail_on, self.description, self._rows = [], None, None, []

    def cursor(self):
        return self

    def close(self):
        pass

    def _sees(self, schema, name):
        return self.owner.get((schema, name)) == self.role or (schema, name) in self.visible

    def _objects(self, schema):
        objects = self.tables if schema == "MARKET" else self.views
        return {n: c for n, c in objects.items() if self._sees(schema, n)}

    def _own(self, schema, name):
        if self.owner.get((schema, name), self.role) != self.role:
            raise RuntimeError("SQL access control error: Insufficient privileges to operate on " + name)

    def execute(self, sql):
        self.sql.append(sql)
        if self.fail_on and self.fail_on in sql:
            self.fail_on = None
            raise RuntimeError(f"failed: {sql}")
        self.description, self._rows = None, []
        if sql == "select current_role()":
            self._rows = [(self.role,)]
        elif m := re.match(r"select table_name, column_name from information_schema.columns .*'(\w+)'$", sql):
            self._rows = [(n.upper(), c) for n, cols in self._objects(m[1]).items() for c in cols]
        elif m := re.match(r"select table_name, table_owner from information_schema.tables .*'(\w+)'$", sql):
            self._rows = [(n.upper(), self.owner[(m[1], n)]) for n in self._objects(m[1])]
        elif sql.startswith("show grants to share"):
            if self.share_owner != self.role:
                raise RuntimeError(NO_ACCESS)
            self.description = [(c,) for c in ("created_on", "privilege", "granted_on", "name", "granted_to")]
            self._rows = [("t", "USAGE", "DATABASE", "COFFEE_DATA", "SHARE")] + [
                ("t", "SELECT", "VIEW", f"COFFEE_DATA.SHARE.{v.upper()}", "SHARE")
                for v in sorted(self.grants)
            ]
        elif m := re.match(r"create table if not exists MARKET\.(\w+)", sql):
            if m[1] in self.tables:
                if not self._sees("MARKET", m[1]):
                    raise RuntimeError(f"SQL compilation error: Object '{m[1].upper()}' already exists.")
            else:
                self.tables[m[1]], self.owner[("MARKET", m[1])] = list(NEW), self.role
        elif m := re.match(r"(drop table if exists|truncate table) MARKET\.(\w+)", sql):
            self._own("MARKET", m[2])
            if m[1].startswith("drop"):
                self.tables.pop(m[2], None)
                self.owner.pop(("MARKET", m[2]), None)
        elif m := re.match(r"select count\(\*\) from MARKET\.(\w+)", sql):
            self._rows = [(self.counts[m[1]],)]
        elif m := re.match(
            r"create or replace secure view SHARE\.(\w+) (copy grants )?as select \* from MARKET\.(\w+)", sql
        ):
            self._own("SHARE", m[1])
            self.views[m[1]], self.owner[("SHARE", m[1])] = list(self.tables[m[3]]), self.role
            if not m[2]:
                self.grants.discard(m[1])  # create or replace without copy grants drops the view's grants
        elif m := re.match(r"grant select on view SHARE\.(\w+) to share", sql):
            if self.share_owner != self.role or m[1] not in self.views:
                raise RuntimeError(NO_ACCESS)
            self._own("SHARE", m[1])
            self.grants.add(m[1])
        return self

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


def export_dir(tmp_path, counts, exported_at="2026-10-27T02:01:48+00:00"):
    import pyarrow as pa
    import pyarrow.parquet as pq

    for t in PUBLIC_TABLES:
        pq.write_table(pa.table({"coffee_id": ["x"], "variant_id": ["v"]}), tmp_path / f"{t}.parquet")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"as_of_run": "2026-10-26", "exported_at": exported_at, "tables": counts})
    )
    return tmp_path


COUNTS = {t: 1 for t in PUBLIC_TABLES}


def current(**kw):
    """A Snowflake set up as admin_setup.sql leaves it, on the current contract."""
    return FakeSnowflake(COUNTS, tables={t: NEW for t in PUBLIC_TABLES}, views={v: NEW for v in VIEWS}, **kw)


def old(**kw):
    """The same, still on the previous contract: a migration is due."""
    return FakeSnowflake(COUNTS, tables={t: OLD for t in PUBLIC_TABLES}, views={v: OLD for v in VIEWS}, **kw)


def healthy(sf):
    owners = all(o == LOADER for o in sf.owner.values())
    return (all(sf.tables.get(t) == NEW for t in PUBLIC_TABLES) and all(sf.views.get(v) == NEW for v in VIEWS)
            and sf.grants == set(VIEWS) and owners)  # fmt: skip


def unchanged(sf):
    writes = ("put ", "drop", "truncate", "copy into", "create or replace", "create table", "grant")
    return not any(s.startswith(writes) for s in sf.sql)


def test_reexporting_a_week_loads_the_new_files(tmp_path):
    """A second export of the same week goes to its own folder, so COPY can't pick up the first one's files."""
    paths = []
    for when in ("2026-10-27T02:01:48+00:00", "2026-10-27T09:30:00+00:00"):
        sf = current()
        snowflake_load.load(export_dir(tmp_path, COUNTS, when), sf)
        copy = next(s for s in sf.sql if s.startswith("copy into MARKET.public_coffee"))
        paths.append(copy.split(" from ")[1].split(" ")[0])
    assert paths == [
        "@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=20261027T020148Z/public_coffee.parquet",
        "@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=20261027T093000Z/public_coffee.parquet",
    ]


def test_load_stages_per_run_and_reloads_each_table(tmp_path):
    counts = {t: i + 1 for i, t in enumerate(PUBLIC_TABLES)}
    sf = FakeSnowflake(counts)  # a first load: every table is created
    assert snowflake_load.load(export_dir(tmp_path, counts), sf) == {"run": "2026-10-26", "tables": counts}
    sql = sf.sql
    puts = [s for s in sql if s.startswith("put ")]
    assert len(puts) == 4 and all("run=2026-10-26/exported=" in p and "overwrite = false" in p for p in puts)
    for t in PUBLIC_TABLES:  # truncate + copy inside one transaction per table
        i = sql.index(f"truncate table MARKET.{t}")
        assert (
            sql[i - 1] == "begin"
            and sql[i + 1].startswith(f"copy into MARKET.{t}")
            and sql[i + 2] == "commit"
        )


def test_row_count_mismatch_fails(tmp_path):
    sf = FakeSnowflake(COUNTS | {"public_coffee": 4})
    with pytest.raises(RuntimeError, match="row counts differ"):
        snowflake_load.load(export_dir(tmp_path, {t: 1 for t in PUBLIC_TABLES}), sf)


def test_same_columns_load_as_usual(tmp_path):
    sf = current()
    assert snowflake_load.load(export_dir(tmp_path, COUNTS), sf)["tables"] == COUNTS
    assert not any(s.startswith(("drop", "create or replace", "grant", "show")) for s in sf.sql)
    assert healthy(sf)


def test_a_changed_public_contract_loads_nothing_until_migrated(tmp_path):
    sf = old()
    with pytest.raises(RuntimeError, match="Nothing was loaded or changed.*public contract changed"):
        snowflake_load.load(export_dir(tmp_path, COUNTS), sf)
    assert unchanged(sf)


def test_migrate_uploads_before_dropping_and_rebuilds_every_view_keeping_its_grants(tmp_path):
    sf = old()
    assert snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)["tables"] == COUNTS
    assert healthy(sf)
    sql = sf.sql
    last_put = max(n for n, s in enumerate(sql) if s.startswith("put "))
    first_drop = min(n for n, s in enumerate(sql) if s.startswith("drop table"))
    assert sql.index(next(s for s in sql if s.startswith("show grants to share"))) < last_put < first_drop
    views = [s for s in sql if s.startswith("create or replace secure view")]
    assert len(views) == 4 and all(" copy grants as select * from MARKET." in v for v in views)
    assert not any(s.startswith("grant") for s in sql)  # copy grants kept every view in the share


@pytest.mark.parametrize(
    "fail_on",
    ["put 'file://", "drop table if exists MARKET.public_roaster", "copy into MARKET.public_price_event",
     "create or replace secure view SHARE.market_weekly"],
)  # fmt: skip
def test_a_migration_that_failed_partway_is_finished_by_running_it_again(tmp_path, fail_on):
    export = export_dir(tmp_path, COUNTS)
    sf = old()
    sf.fail_on = fail_on
    with pytest.raises(RuntimeError, match="failed"):
        snowflake_load.load(export, sf, migrate=True)
    assert sf.grants == set(VIEWS)  # copy grants: no view left the share, even mid-way
    if not healthy(sf):  # an ordinary weekly load refuses a half-migrated Snowflake rather than loading NULLs
        with pytest.raises(RuntimeError, match="public contract changed"):
            snowflake_load.load(export, sf)
    snowflake_load.load(export, sf, migrate=True)
    snowflake_load.load(export, sf)  # and the weekly load works again
    assert healthy(sf)


def test_a_view_recreated_as_accountadmin_stops_the_next_migration(tmp_path):
    """A migration run as ACCOUNTADMIN leaves it owning every view it recreated, and COFFEE_LOADER then can't see
    any of them. They are still in the share, so the share's own grants show they exist."""
    sf = old()
    for v in VIEWS:
        sf.owner[("SHARE", v)] = "ACCOUNTADMIN"
    with pytest.raises(
        RuntimeError, match="SHARE.coffee is in share COFFEE_DATA_SHARE but COFFEE_LOADER can't see it"
    ):
        snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert unchanged(sf)
    for v in VIEWS:  # given back
        sf.owner[("SHARE", v)] = LOADER
    snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert healthy(sf)


def test_a_visible_view_another_role_owns_stops_the_migration(tmp_path):
    sf = old()
    sf.owner[("SHARE", "roaster")] = "ACCOUNTADMIN"
    sf.visible.add(("SHARE", "roaster"))  # COFFEE_LOADER can select from it, but can't replace it
    with pytest.raises(RuntimeError, match="SHARE.roaster is owned by ACCOUNTADMIN, not COFFEE_LOADER.*grant "
                                           "ownership on view SHARE.roaster to role COFFEE_LOADER copy current"):  # fmt: skip
        snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert unchanged(sf)


def test_a_new_public_table_gets_its_table_view_and_grant(tmp_path):
    """A public table added since admin_setup.sql part 2: no table, no view, not in the share. That's new, not hidden."""
    sf = current()
    del sf.tables["public_roaster"], sf.owner[("MARKET", "public_roaster")]
    del sf.views["roaster"], sf.owner[("SHARE", "roaster")]
    sf.grants.discard("roaster")
    snowflake_load.load(export_dir(tmp_path, COUNTS), sf)  # the weekly load creates the table
    assert sf.tables["public_roaster"] == NEW and "roaster" not in sf.views
    snowflake_load.load(
        export_dir(tmp_path, COUNTS), sf, migrate=True
    )  # a migration adds its view to the share
    assert healthy(sf)


def test_a_view_missing_from_the_share_is_granted_before_anything_changes(tmp_path):
    sf = old(shared={"coffee", "price_event", "market_weekly"})
    snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert healthy(sf)
    grant = sf.sql.index("grant select on view SHARE.roaster to share COFFEE_DATA_SHARE")
    assert grant < min(n for n, s in enumerate(sf.sql) if s.startswith("put "))


def test_share_views_without_a_share_this_role_can_see_stop_the_migration(tmp_path):
    sf = old()
    sf.share_owner = "ACCOUNTADMIN"
    with pytest.raises(RuntimeError, match="COFFEE_LOADER can't see share COFFEE_DATA_SHARE"):
        snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert unchanged(sf)


def test_migrate_before_the_share_and_its_views_exist_only_reloads_the_tables(tmp_path):
    sf = FakeSnowflake(COUNTS, tables={t: OLD for t in PUBLIC_TABLES}, share=False)
    snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert all(sf.tables[t] == NEW for t in PUBLIC_TABLES) and sf.views == {}
    assert not any(s.startswith(("grant", "create or replace secure view")) for s in sf.sql)


@pytest.mark.parametrize("visible", [True, False])
def test_a_table_another_role_owns_stops_the_load_before_any_table_changes(tmp_path, visible):
    sf = current()
    sf.owner[("MARKET", "public_roaster")] = "ACCOUNTADMIN"
    if visible:
        sf.visible.add(("MARKET", "public_roaster"))
    with pytest.raises(RuntimeError, match="MARKET.public_roaster .*grant ownership on table "
                                           "MARKET.public_roaster to role COFFEE_LOADER"):  # fmt: skip
        snowflake_load.load(export_dir(tmp_path, COUNTS), sf)
    assert not any(s.startswith(("drop", "truncate", "copy into")) for s in sf.sql)


def test_a_view_another_role_owns_outside_the_share_fails_before_anything_is_loaded(tmp_path):
    sf = old(shared={"coffee", "price_event", "market_weekly"})
    sf.owner[("SHARE", "roaster")] = "ACCOUNTADMIN"  # invisible and not in the share: it looks new
    with pytest.raises(RuntimeError, match="Insufficient privileges"):
        snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True)
    assert not any(s.startswith(("drop", "truncate", "copy into")) for s in sf.sql)
    assert all(sf.tables[t] == OLD for t in PUBLIC_TABLES)


def test_dry_run_changes_nothing_and_shows_the_plan(tmp_path):
    sf = old(shared={"coffee", "price_event", "market_weekly"})
    sf.owner[("SHARE", "roaster")] = "ACCOUNTADMIN"
    out = snowflake_load.load(export_dir(tmp_path, COUNTS), sf, migrate=True, dry_run=True)
    assert unchanged(sf)
    assert out["role"] == LOADER and out["share"] == "COFFEE_DATA_SHARE"
    assert out["changed"] == list(PUBLIC_TABLES) and out["views"]["public_coffee"] == "stale"
    assert out["views"]["public_roaster"] == "new"  # invisible and not in the share
    assert any(s.startswith("drop table if exists MARKET.public_coffee") for s in out["statements"])
    clean = snowflake_load.load(export_dir(tmp_path, COUNTS), old(), migrate=True, dry_run=True)
    assert clean["problems"] == [] and clean["grants"] == []


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
