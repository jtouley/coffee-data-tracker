"""Tier 1 of reproducible releases: the same lake, built twice through `coffeedata build`, gives the same release.

The second build reads a copy of the lake with every raw file's rows, and the files in each folder, in reverse
order, so a result that depends on the order DuckDB happens to read rows in (an unordered list or string_agg, a
row_number() or arg_max() tie, a set's order in a Python model) shows up as a difference instead of passing by luck.
The lake holds such ties on purpose. Only the per-build columns (quality.PER_BUILD_COLUMNS: which build a row came
from) may differ. Tier 2, the content hash each build records per release table, is checked on the same two builds.
"""

import argparse
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from coffeedata import ingest, products, quality
from coffeedata.lake import Lake

from .lakehelp import at, manifest, probe, product, run

pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
WEEKS = ("2026-10-19", "2026-10-26")
VERDICT_MODEL = "claude-test-model"
ONYX = "onyxcoffeelab.com"
PARTIAL, EDGE = "partial.example", "edge.example"
GB = "blacksheepcoffee.co.uk"  # in the seed list, country GB


def _write(lake: Lake, run_date: str, results: list[ingest.Result], bodies: set[str], batch: str) -> None:
    for table, rows in ingest.rows_for(results, date.fromisoformat(run_date), batch, bodies, "test").items():
        lake.write_partition(table, run_date, batch, rows)


def _shot(lake: Lake, shot_id: str, brewed: datetime, grind: float, rating: int, label: str) -> None:
    lake.write("shots", f"shots/{shot_id}__20261022T060000.parquet", [{
        "shot_id": shot_id, "logged_at": at("2026-10-22", 6), "deleted": False, "brewed_at": brewed,
        "coffee_freetext": label, "grinder_setting": grind, "dose_g": 18.0, "yield_g": 36.0, "time_s": 28.0,
        "brew_method": "espresso", "rating": rating, "is_milk_drink": False}])  # fmt: skip


def fixture_lake(root: Path, fixture_items: dict, roasters: dict, ties: bool = True) -> Lake:
    """Two weekly runs of the five recorded storefronts (week two: the first product of each gone, every third
    one's price up), with what a real lake also holds: probes, run manifests, registry rows from two sources, a
    failed fetch, an override, stored Claude verdicts, a Claude enrichment and a few shots. And a tie for every
    "latest" pick the build makes (and, from #107, a bag priced in CAD and a roaster outside the US): two ok batches of one roaster-week fetched in the same second, two partial and two
    challenged fetches in the same second, two probes in the same second, two registry rows added in the same
    second, two verdicts and two enrichments stored in the same second, two equally rated shots. Without `ties`, the same lake with none of them (where any build of it, before
    the tie-breaks or after, gives one answer)."""
    lake = Lake(str(root))
    bodies: set[str] = set()
    for w, d in enumerate(WEEKS):
        results = []
        for rid, items in fixture_items.items():
            if w:
                items = [
                    i.model_copy(update={"variants": [v.model_copy(update={"price_usd": (v.price_usd or 0) + 1})
                                                      for v in i.variants]}) if n % 3 == 0 else i
                    for n, i in enumerate(items[1:])
                ]  # fmt: skip
                if rid == next(iter(fixture_items)):  # one US bag states another currency in week two
                    items = [items[0].model_copy(update={"variants": [
                        v.model_copy(update={"currency": "CAD"}) for v in items[0].variants]}), *items[1:]]  # fmt: skip
            results.append(ingest.Result(roasters[rid], at(d), items))
            probe(lake, d, rid, platform=roasters[rid].platform)
        _write(lake, d, results, bodies, f"w{w}")
        run(lake, d, "down.example", None)  # a store whose fetch failed both weeks
        gb = product("gb1", 12.0 + w)  # a roaster outside the US, priced in pounds (never published)
        gb["variants"][0]["currency"] = "GBP"
        run(lake, d, GB, [gb])
        manifest(lake, d, [*fixture_items, "down.example"])
    # Ties. Another ok batch of Onyx's week two in the same second as w1 (fetched_at at(WEEKS[1])), with other
    # products and another adapter: which batch counts decides Onyx's catalog. A second probe of Intelligentsia in
    # the same second.
    if ties:
        run(lake, WEEKS[1], ONYX, [product("tie1"), product("tie2")], adapter="generic")
        probe(lake, WEEKS[1], "intelligentsia.com", status="timeout")
        # Two partial ok fetches of one store in the same second, with different reasons, and two failed fetches
        # of another answered by different edges' challenges (int_roaster_runs: partial_reason, challenge).
        for reason in ("partial: page cap", "partial: deadline"):
            run(lake, WEEKS[1], PARTIAL, [product("p1")], partial=True, error=reason)
        for block in ({"sg_captcha": "1"}, {"cf_mitigated": "challenge"}):
            run(lake, WEEKS[1], EDGE, None, blocks=[{"status": 403, "host": EDGE, "path": "/", **block}])
    lake.write("registry", "registry/source=overture/x.parquet", [
        {"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": "vividcoffee.com",
         "name": "Vivid Coffee", "state": "CA", "locations": 2},
        {"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": "anaheimcoffee.com",
         "name": "Anaheim Coffee", "city": "Anaheim", "state": "CA", "locations": 1},
        *([{"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": "anaheimcoffee.com",
           "name": "Anaheim Roasting Company", "city": "Anaheim", "state": "CA", "locations": 1}] if ties else []),
        {"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": "down.example",
         "name": "Down", "state": "OR", "locations": 1}])  # fmt: skip
    lake.write("registry", "registry/source=osm/x.parquet", [
        {"added_at": datetime(2026, 10, 2, tzinfo=UTC), "source": "osm", "roaster_id": "vividcoffee.com",
         "name": "Vivid Coffee Roasters", "state": "CA", "locations": 2}])  # fmt: skip
    quality.add_override(lake, ONYX, WEEKS[1], "accept", "checked by hand", None)
    # Three of week two's coffees (by the rules), with descriptions: the verdicts and the enrichment are about them.
    rows = (
        lake.connect()
        .execute(
            f"select {products.input_hash_sql('s')} as h, s.roaster_id, s.product_id, s.body_hash, s.title, "
            f"s.product_type, s.tags, list_transform(s.variants, lambda v: v.grams), b.body_text "
            f"from {lake.scan('snapshots')} s join {lake.scan('bodies')} b using (body_hash) "
            "where s.batch_id = 'w1' order by s.roaster_id, s.product_id"
        )
        .fetchall()
    )
    snap, seen = [], set()
    for h, rid, pid, body, *item in rows:
        if h not in seen and products.decide(products.item(*item[:4], body, item[4]), None)[0]:
            snap.append((h, rid, pid, body))
            seen.add(h)
    snap = snap[:3]
    definition = _definition()
    lake.write_partition("verdicts", WEEKS[1], "v1", [
        {"decided_at": at(WEEKS[1]), "input_hash": h, "model": VERDICT_MODEL, "definition": definition,
         "label": label}
        for (h, *_), label in zip([*snap, snap[0]], ("not_coffee", "coffee", "unsure", "coffee"), strict=True)
        if ties or label != "not_coffee"])  # fmt: skip
    _, rid, pid, body = snap[0]
    lake.write_partition("enrichments", WEEKS[1], "e1", [
        {"extracted_at": at(WEEKS[1]), "roaster_id": rid, "product_id": pid, "body_hash": body, "method": "llm",
         "needs_llm": False, "origin_country": country, "process": "washed", "roast_level": "light",
         "tasting_notes": ["peach", "jasmine", "honey"], "varietals": ["Heirloom"]}
        for country in ("Ethiopia", "Kenya")[: 2 if ties else 1]])  # fmt: skip
    # One coffee, written three ways (one key: lower-cased); with ties, two shots share the top rating.
    _shot(lake, "s1", at("2026-10-20", 7), 10.0, 4, "House Espresso")
    name2, name3 = ("house espresso", "House espresso") if ties else ("House Espresso", "House Espresso")
    _shot(lake, "s2", at("2026-10-21", 7), 12.0, 4 if ties else 3, name2)
    _shot(lake, "s3", at("2026-10-22", 7), 14.0, 2, name3)
    return lake


def _definition() -> str:
    from coffeedata import verdict

    return verdict.DEFINITION_VERSION


def reversed_copy(src: Path, dst: Path) -> None:
    """The same lake, read in the opposite order: every data file's rows reversed, and every folder's files renamed
    so they sort the other way round (the build reads tables by glob, and file names carry no data)."""
    shutil.copytree(src, dst)
    for folder in sorted({p.parent for p in dst.rglob("*.parquet")}):
        files = sorted(folder.glob("*.parquet"))
        for i, p in enumerate(files):
            t = pq.read_table(p)
            if t.num_rows > 1:
                pq.write_table(t.take(list(range(t.num_rows - 1, -1, -1))), p)
            p.rename(p.with_name(f"{len(files) - i:04d}-{p.name}"))


def build(monkeypatch, lake_root: Path, db: Path) -> str:
    """One `coffeedata build` of the lake at lake_root into db; returns its build id."""
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake_root))
    monkeypatch.setattr(cli, "SERVED_DB", db.with_name(db.stem + "-served.duckdb"))
    args = argparse.Namespace(db=str(db), run_date=WEEKS[1], accept_volume_change=False)
    cli.cmd_build(args)  # passing or not, every build records its release and its hashes
    return quality.builds(Lake())[-1][0]


def differences(a: duckdb.DuckDBPyConnection, table: str) -> tuple[int, int, list, list]:
    """Rows of `table` only in database a, only in the attached database z (EXCEPT ALL both ways, per-build
    columns aside), and a sample of each."""
    cols = [r[0] for r in a.execute(f"describe {table}").fetchall() if r[0] not in quality.PER_BUILD_COLUMNS]
    sel = ", ".join(f'"{c}"' for c in cols)
    only = []
    for x, y in (("main", "z"), ("z", "main")):
        q = f"select {sel} from {x}.{table} except all select {sel} from {y}.{table}"
        only.append(
            (a.execute(f"select count(*) from ({q})").fetchone()[0], a.execute(f"{q} limit 1").fetchall())
        )
    return only[0][0], only[1][0], only[0][1], only[1][1]


def test_the_same_lake_builds_the_same_release(fixture_items, roasters, monkeypatch, tmp_path, capsys):
    from coffeedata import cli

    real = cli.dbt_build
    # The real seed list has ~3,200 roasters this lake never fetches (completeness has its own tests), so the build
    # passes and is served, like a real weekly one.
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}))  # fmt: skip
    monkeypatch.setattr(cli, "_today", lambda: WEEKS[1])
    monkeypatch.setattr(cli, "MARTS", list(quality.SERVED_MARTS))
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", VERDICT_MODEL)
    first, second = tmp_path / "lake-a", tmp_path / "lake-b"
    fixture_lake(first, fixture_items, roasters)
    reversed_copy(first, second)
    a_id = build(monkeypatch, first, tmp_path / "a.duckdb")
    # The second build also runs in another time zone, as a rebuild on someone's laptop does (RUNBOOK §5.6).
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    z_id = build(monkeypatch, second, tmp_path / "z.duckdb")
    monkeypatch.delenv("TZ")
    assert a_id != z_id

    con = duckdb.connect(str(tmp_path / "a.duckdb"), read_only=True)
    con.execute(f"attach '{tmp_path / 'z.duckdb'}' as z (read_only)")
    tables = [r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_catalog = 'a' and table_schema = 'main' "
        "and table_type = 'BASE TABLE' order by 1").fetchall()]  # fmt: skip
    # Every release table, and every model table they're built from (views read the lake, so they're left out;
    # dbt's stored test failures are in their own schema).
    assert set(quality.RELEASE_TABLES) <= set(tables) and "stg_products" in tables
    differ = []
    for t in tables:
        n_a, n_z, row_a, row_z = differences(con, t)
        if n_a or n_z:
            differ.append(f"{t}: {n_a} rows only in the first build (e.g. {row_a}), {n_z} only in the second "
                          f"(e.g. {row_z})")  # fmt: skip
    assert not differ, "two builds of the same lake differ:\n" + "\n".join(differ)
    # The table that carries the build id really does carry it, and nothing else told them apart.
    ids = {r[0] for r in con.execute("select distinct release_build_id from public_coffee union all "
                                     "select distinct release_build_id from z.public_coffee").fetchall()}  # fmt: skip
    assert ids == {a_id, z_id}
    assert con.execute("select count(*) from public_coffee").fetchone()[0] > 20  # the storefronts' coffees

    # Tier 2: each build recorded every release table's content hash and row count, and they agree.
    first_hashes = quality.build_hashes(Lake(str(first)), a_id)
    second_hashes = quality.build_hashes(Lake(str(second)), z_id)
    assert set(first_hashes) == set(quality.RELEASE_TABLES)
    assert first_hashes == second_hashes
    for t, (rows, *_) in first_hashes.items():
        assert rows == con.execute(f"select count(*) from {t}").fetchone()[0], t

    # Each tie went the documented way (the same in both builds, checked above).
    one = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
    week2 = f"roaster_id = '{ONYX}' and run_date = '{WEEKS[1]}'"
    # The later batch id of two fetched in the same second (stg_run_walk), so Onyx's catalog is w1's, not tie1/2's,
    # and the week's adapter, of two attempts in the same second, by the value (int_roaster_runs).
    assert one(f"select batch_id from stg_runs where {week2}") == "w1"
    assert one(f"select count(*) from dim_coffee where roaster_id = '{ONYX}' and product_id like 'tie%'") == 0
    assert one(f"select adapter from int_roaster_runs where {week2}") == "shopify"
    # Two partial fetches, or two challenged ones, in the same second: the greater value (int_roaster_runs).
    assert (
        one(f"select partial_reason from int_roaster_runs where roaster_id = '{PARTIAL}'")
        == "partial: page cap"
    )
    assert one(f"select challenge from int_roaster_runs where roaster_id = '{EDGE}'") == "siteground"
    # The lake's sources alphabetically after the seed list's (stg_registry), and a tie in added_at by the value.
    vivid = one("select sources from dim_roaster where roaster_id = 'vividcoffee.com'")
    assert vivid == "curated+overture+osm+overture"
    assert (
        one("select roaster_name from dim_roaster where roaster_id = 'anaheimcoffee.com'")
        == "Anaheim Roasting Company"
    )
    # Two verdicts in the same second: the label decides (coffee before not_coffee).
    labels = con.execute("select label, count(*) from stg_verdicts group by 1 order by 1").fetchall()
    assert labels == [("coffee", 2), ("unsure", 1)]
    # Two enrichments in the same second: one of them, always the same (both builds agree above).
    assert one("select count(*) from dim_coffee where enrichment_method = 'llm'") == 1
    # Prices in another currency (stg_snapshots.is_usd): the CAD bag is in the marts but its week-two price is no
    # price change, and the GB roaster's coffee is in dim_coffee but in no public table.
    cad = one("select coffee_id from stg_snapshots, unnest(variants) t(v) where v.currency = 'CAD' limit 1")
    assert one("select count(*) from stg_snapshots, unnest(variants) t(v) where not v.is_usd") >= 3
    assert one(f"select count(*) from fct_price_event where coffee_id = '{cad}' and event = 'price'") == 0
    assert (
        one(f"select count(*) from public_price_event where coffee_id = '{cad}' and run_date = '{WEEKS[1]}'")
        == 0
    )
    assert one(f"select roaster_country from dim_coffee where roaster_id = '{GB}'") == "GB"
    assert one(f"select count(*) from public_coffee where roaster_id = '{GB}'") == 0
    assert one(f"select count(*) from public_roaster where roaster_id = '{GB}'") == 0
    # The best recipe is the latest of the equally top-rated shots (s2), and the label the latest shot's spelling (s3).
    dial = con.execute(
        "select best_grind, best_rating, label, shots from mart_dial_in where grain = 'coffee'"
    )
    assert dial.fetchall() == [(12.0, 4, "House espresso", 3)]

    # build-hashes, as a verification uses it (RUNBOOK): the rebuild's record beside the original's, in one lake.
    (rebuilt,) = second.glob(f"builds/*/build={z_id}.parquet")
    shutil.copy(rebuilt, first / rebuilt.relative_to(second))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(first))
    capsys.readouterr()
    cli.main(["build-hashes", a_id, "--against", z_id, "--verify"])  # no SystemExit: the same release
    out = json.loads(capsys.readouterr().out)
    assert out["against"]["same"] and out["verify"]["same"]
    assert set(out["tables"]) == set(quality.RELEASE_TABLES)
    inputs = out["inputs"]  # what a rebuild has to match, from the build's own record
    assert inputs["run_date"] == WEEKS[1] and inputs["dbt_vars"]["verdict_model"] == VERDICT_MODEL
    assert inputs["dbt_version"] == inputs["package_versions"]["dbt-core"]
    # A release file that no longer matches its record (one row gone) is named, and the command exits 1.
    (served,) = first.glob(f"releases/*/build={a_id}/public_roaster.parquet")
    t = pq.read_table(served)
    pq.write_table(t.slice(1), served)
    with pytest.raises(SystemExit):
        cli.main(["build-hashes", a_id, "--verify"])
    bad = json.loads(capsys.readouterr().out)["verify"]
    assert [k for k, d in bad["tables"].items() if not d["same"]] == ["public_roaster"]
    assert bad["tables"]["public_roaster"]["rows"] == {"old": t.num_rows, "new": t.num_rows - 1}


def test_table_hash_ignores_row_order_and_build_ids_but_nothing_else():
    con = duckdb.connect()
    con.execute("create table a as select * from (values (1, 'x', [1, 2], 'b1'), (2, 'y', [3], 'b1')) "
                "t(n, s, l, release_build_id)")  # fmt: skip
    base = quality.table_hash(con, "a")
    assert base[0] == 2
    same = [
        "select * from a order by n desc",  # row order
        "select * replace ('b2' as release_build_id) from a",  # the build id
        "select l, release_build_id, s, n from a",  # column order
    ]
    for q in same:
        assert quality.table_hash(con, f"({q})") == base, q
    differ = [
        "select * replace (if(n = 1, 'z', s) as s) from a",  # one value
        "select * replace (if(n = 1, [2, 1], l) as l) from a",  # a list's order
        "select * from a union all select * from a where n = 1",  # a duplicate row
        "select * from a where n = 1",  # a missing row
        "select n as m, s, l, release_build_id from a",  # a column renamed
        "select *, 1 as extra from a",  # a column added
    ]
    for q in differ:
        assert quality.table_hash(con, f"({q})")[1] != base[1], q
    # Timestamps hash the same whatever the session's time zone.
    con.execute("create table ts as select timestamptz '2026-10-26 12:00:00+00' as t")
    con.execute("set TimeZone = 'America/Los_Angeles'")
    la = quality.table_hash(con, "ts")
    con.execute("set TimeZone = 'UTC'")
    assert quality.table_hash(con, "ts") == la
    # Two methods' hashes are never compared as content.
    assert quality.compare_hashes({"t": (1, "h", "m1")}, {"t": (1, "h2", "m2")})["t"] == {
        "same": False, "rows": {"old": 1, "new": 1}, "method": {"old": "m1", "new": "m2"}}  # fmt: skip
