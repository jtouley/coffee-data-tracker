"""Build the dbt project over a synthetic lake and check the derived history (the same SQL the weekly run uses)."""

import json
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import duckdb
import pytest

from coffeedata import registry

from .lakehelp import probe, product, run

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")


def build(lake, tmp_path):
    db = tmp_path / "coffee.duckdb"
    if db.exists():
        db.unlink()
    r = subprocess.run(
        [
            "dbt",
            "build",
            "--project-dir",
            "dbt",
            "--profiles-dir",
            "dbt",
            "--vars",
            json.dumps({"lake_roots": [str(lake.root)], "config_dir": str(FIX)}),
            "--target-path",
            str(tmp_path / "target"),
            "--log-path",
            str(tmp_path / "logs"),
        ],
        cwd=ROOT,
        env=os.environ | {"COFFEEDATA_DB": str(db)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout[-4000:]
    return duckdb.connect(str(db), read_only=True)


def test_history_is_derived_from_successful_runs(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    run(
        lake,
        "2026-10-19",
        rid,
        [product("A", 20.0), product("B", 18.0), product("M", coffee=False, title="Mug")],
    )
    run(lake, "2026-10-26", rid, None)  # failed fetch: must not look like a delisting
    run(lake, "2026-11-02", rid, [product("A", 22.0), product("C", 25.0)])
    con = build(lake, tmp_path)

    events = sorted(
        con.execute(
            "select run_date, coffee_id, event, price_usd, previous_price_usd from fct_price_event"
        ).fetchall()
    )
    d1, d3 = date(2026, 10, 19), date(2026, 11, 2)
    assert events == [
        (d1, f"{rid}:A", "new", 20.0, None),
        (d1, f"{rid}:B", "new", 18.0, None),
        (d3, f"{rid}:A", "price", 22.0, 20.0),
        (d3, f"{rid}:B", "removed", None, 18.0),
        (d3, f"{rid}:C", "new", 25.0, None),
    ]
    coffees = {
        r[0]: r[1:]
        for r in con.execute(
            "select product_id, is_listed, in_stock, first_seen, last_seen from dim_coffee"
        ).fetchall()
    }
    assert coffees["B"] == (False, False, d1, d1)  # delisted but kept: history still resolves (finding 6)
    assert coffees["A"][:2] == (True, True) and "M" not in coffees
    assert con.execute("select status from dim_roaster where roaster_id = ?", [rid]).fetchone()[0] == "active"
    assert con.execute("select count(*) from mart_new_arrivals").fetchone()[0] == 1  # C

    # Re-running a week (another successful batch the same day) changes nothing downstream.
    run(lake, "2026-11-02", rid, [product("A", 22.0), product("C", 25.0)], hour=18)
    con2 = build(lake, tmp_path)
    assert (
        sorted(
            con2.execute(
                "select run_date, coffee_id, event, price_usd, previous_price_usd from fct_price_event"
            ).fetchall()
        )
        == events
    )


def test_relisting(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    run(lake, "2026-10-19", rid, [product("A"), product("B")])
    run(lake, "2026-10-26", rid, [product("A")])
    run(lake, "2026-11-02", rid, [product("A"), product("B")])
    con = build(lake, tmp_path)
    b = con.execute(
        "select run_date, event from fct_price_event where coffee_id like '%:B' order by 1"
    ).fetchall()
    assert b == [
        (date(2026, 10, 19), "new"),
        (date(2026, 10, 26), "removed"),
        (date(2026, 11, 2), "relisted"),
    ]


def test_repeated_variant_ids_in_raw_snapshots(lake, tmp_path):
    # Raw snapshots written before the generic adapter de-duplicated SKUs: same variant_id on every offer.
    p = product("A")
    p["variants"] = [
        p["variants"][0] | {"variant_id": "SKU", "title": t} for t in ("Whole bean", "Drip", "Espresso")
    ]
    run(lake, "2026-10-26", "ikoncoffee.com", [p])
    con = build(lake, tmp_path)  # unique_price_events would fail without the staging fix
    assert sorted(con.execute("select variant_id, event from fct_price_event").fetchall()) == [
        ("SKU", "new"),
        ("SKU#1", "new"),
        ("SKU#2", "new"),
    ]


def test_python_and_dbt_agree_on_roaster_status(lake, tmp_path):
    a, b, c = "onyxcoffeelab.com", "intelligentsia.com", "vividcoffee.com"
    run(lake, "2026-10-19", a, [product("1")])
    run(lake, "2026-10-19", b, [product("1", coffee=False)])
    for d in ("2026-10-19", "2026-10-26", "2026-11-02"):
        run(lake, d, c, None)
    probe(lake, "2026-11-03", a, status="unreachable")
    con = build(lake, tmp_path)
    dbt_status = dict(con.execute("select roaster_id, status from dim_roaster").fetchall())
    py_status = {s.roaster.roaster_id: s.status for s in registry.current(lake, FIX / "roasters.csv")}
    assert dbt_status == py_status
    assert (py_status[a], py_status[b], py_status[c]) == ("active", "no_coffee", "unreachable")


def test_public_tables_are_facts_only_and_filtered(lake, tmp_path):
    from datetime import UTC, datetime

    from coffeedata import public

    now = datetime(2026, 10, 1, tzinfo=UTC)
    lake.write(
        "registry",
        "registry/source=osm/x.parquet",
        [{"added_at": now, "source": "osm", "roaster_id": "osmonly.coffee", "name": "OSM Only"}],
    )
    lake.write(
        "registry",
        "registry/source=manual/y.parquet",
        [{"added_at": now, "source": "manual", "roaster_id": "vividcoffee.com", "opted_out": True}],
    )
    for rid in ("onyxcoffeelab.com", "osmonly.coffee", "vividcoffee.com"):
        probe(lake, "2026-10-18", rid)
        run(lake, "2026-10-19", rid, [product("A"), product("M", coffee=False)])
    con = build(lake, tmp_path)

    roasters = {r[0] for r in con.execute("select roaster_id from public_roaster").fetchall()}
    assert "onyxcoffeelab.com" in roasters
    assert "osmonly.coffee" not in roasters and "vividcoffee.com" not in roasters  # ODbL-only, opted out
    assert {r[0] for r in con.execute("select distinct roaster_id from public_coffee").fetchall()} == {
        "onyxcoffeelab.com"
    }
    cols = {
        r[0]
        for r in con.execute(
            "select column_name from information_schema.columns where table_name = 'public_coffee'"
        ).fetchall()
    }
    assert "source_url" in cols and not cols & {"image", "handle", "product_id", "body_hash", "tags"}
    con.close()

    out = public.export(tmp_path / "coffee.duckdb", str(tmp_path / "public"))
    assert out["tables"]["public_coffee"] == 1
    assert sorted(p.name for p in (tmp_path / "public").iterdir()) == [
        "manifest.json",
        "public_coffee.parquet",
        "public_market_weekly.parquet",
        "public_price_event.parquet",
        "public_roaster.parquet",
    ]


def test_old_and_new_shots_with_equipment_and_bags(lake, tmp_path):
    from datetime import datetime

    from coffeedata.lake import SCHEMAS
    from coffeedata.models import Bag, Equipment, Shot
    from coffeedata.shots import record

    from .lakehelp import at, old_shot

    rid = "onyxcoffeelab.com"
    run(lake, "2026-10-19", rid, [product("A", title="Geometry")])
    old_shot(lake, "old1", "2026-10-20", coffee_id=f"{rid}:A", rating=4)
    old_shot(lake, "old2", "2026-10-21", coffee_freetext="Cafe bag", time_s=22.0)

    def equip(e: Equipment, when: datetime) -> None:
        lake.write(
            "equipment",
            f"equipment/{e.equipment_id}__{when:%Y%m%dT%H%M%S}.parquet",
            [e.model_dump() | {"logged_at": when}],
        )

    g = Equipment(
        equipment_id="g1",
        user_id="u1",
        kind="grinder",
        name="Grinder",
        scale_min=0,
        scale_max=90,
        scale_step=0.5,
    )
    equip(g, at("2026-10-22", 6))
    equip(g.model_copy(update={"name": "DF64 V2"}), at("2026-10-22", 7))  # a correction: latest wins
    bag = Bag(
        bag_id="b1",
        user_id="u1",
        coffee_id=f"{rid}:A",
        coffee_freetext="Onyx Geometry",
        roast_date="2026-10-15",
    )
    lake.write(
        "bags", "bags/b1__20261022T060000.parquet", [bag.model_dump() | {"logged_at": at("2026-10-22", 6)}]
    )
    for i, (grind, rating) in enumerate([(11.0, 3), (10.0, 5)]):
        record(
            Shot(
                shot_id=f"new{i}",
                brewed_at=at("2026-10-23", 7 + i),
                grinder_setting=grind,
                dose_g=18,
                yield_g=36,
                time_s=29,
                rating=rating,
                user_id="u1",
                grinder_id="g1",
                bag_id="b1",
                source="app",
            ),
            lake,
        )
    assert set(SCHEMAS["shots"].names) - {"logged_at", "deleted"} <= set(Shot.model_fields) | {"logged_at"}

    con = build(lake, tmp_path)
    rows = {
        r[0]: r[1:]
        for r in con.execute(
            "select shot_id, user_id, coffee_id, roast_date, grinder_name, grind_position, source from fct_shot"
        ).fetchall()
    }
    assert rows["old1"] == ("owner", f"{rid}:A", None, None, None, None)  # unchanged; the owner's
    assert rows["old2"][:2] == ("owner", None)
    # The bag supplies the roast date; the grinder its name and scale.
    assert rows["new1"] == ("u1", f"{rid}:A", date(2026, 10, 15), "DF64 V2", round(10 / 90, 3), "app")
    dial = con.execute(
        "select user_id, grinder_name, shots, best_grind from mart_dial_in where grain = 'coffee' order by 1, 2"
    ).fetchall()
    assert dial == [("owner", None, 1, 12.0), ("owner", None, 1, 12.0), ("u1", "DF64 V2", 2, 10.0)]
    assert con.execute("select count(*) from dim_equipment").fetchone()[0] == 1


def test_only_old_shot_files_still_build(lake, tmp_path):
    """A lake whose shots all predate the app's columns: the v2 schema file supplies them as null."""
    from .lakehelp import old_shot

    old_shot(lake, "old1", "2026-10-20", coffee_freetext="x")
    con = build(lake, tmp_path)
    assert con.execute("select shot_id, user_id, grinder_id, bag_id from fct_shot").fetchall() == [
        ("old1", "owner", None, None)
    ]


def test_site_has_no_shots(lake, tmp_path):
    from coffeedata import site

    from .lakehelp import old_shot

    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A")])
    old_shot(lake, "secretshot", "2026-10-20", coffee_freetext="Private Blend", notes="kitchen notes")
    build(lake, tmp_path).close()
    out = site.build(tmp_path / "coffee.duckdb", tmp_path / "site")
    text = "".join(p.read_text() for p in out.parent.iterdir() if p.is_file()).lower()
    assert not any(w in text for w in ("shot", "private blend", "kitchen notes", "rating"))
