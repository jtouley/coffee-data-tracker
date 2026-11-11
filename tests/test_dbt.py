"""Build the dbt project over a synthetic lake and check the derived history (the same SQL the weekly run uses)."""

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from coffeedata import registry

from .lakehelp import needs_dbt, probe, product, run

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
pytestmark = needs_dbt


def run_dbt(lake, tmp_path, *args, **extra_vars):
    return subprocess.run(
        [
            "dbt",
            *args,
            "--project-dir",
            "dbt",
            "--profiles-dir",
            "dbt",
            "--vars",
            json.dumps({"lake_roots": [str(lake.root)], "config_dir": str(FIX)} | extra_vars),
            "--target-path",
            str(tmp_path / "target"),
            "--log-path",
            str(tmp_path / "logs"),
        ],
        cwd=ROOT,
        env=os.environ | {"COFFEEDATA_DB": str(tmp_path / "coffee.duckdb")},
        capture_output=True,
        text=True,
    )


def build(lake, tmp_path, check=True, **extra_vars):
    db = tmp_path / "coffee.duckdb"
    if db.exists():
        db.unlink()
    # Like the weekly build, without dbt's unit tests: test_dbt_unit_tests runs those.
    r = run_dbt(lake, tmp_path, "build", "--exclude", "resource_type:unit_test", **extra_vars)
    if not check:
        return r
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


def test_new_snapshot_column_keeps_raw_immutable_and_builds_idempotent(lake, tmp_path, monkeypatch):
    import hashlib

    from coffeedata import lake as lake_mod

    rid = "onyxcoffeelab.com"
    # Week 1 written before snapshots had published_at; week 2 after.
    old = lake_mod.SCHEMAS["snapshots"]
    monkeypatch.setitem(lake_mod.SCHEMAS, "snapshots", old.remove(old.get_field_index("published_at")))
    run(lake, "2026-10-19", rid, [product("A"), product("B", available=False) | {"tags": ["Archived"]}])
    monkeypatch.setitem(lake_mod.SCHEMAS, "snapshots", old)
    pub = datetime(2024, 3, 1, 14, 30, tzinfo=UTC)
    run(lake, "2026-10-26", rid, [product("A") | {"published_at": pub}, product("C") | {"published_at": pub}])

    def raw() -> dict[str, str]:  # every data file in the lake, by content (schema files are zero-row)
        return {str(p.relative_to(lake.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in lake.root.rglob("*.parquet") if "schema" not in p.name}  # fmt: skip

    def marts(con, observed: bool = True) -> dict[str, list]:
        cols = {m: "*" for m in ("dim_coffee", "public_coffee")}
        cols["fct_price_event"] = "*" if observed else "* exclude (fetched_at)"
        return {m: con.execute(f"select {c} from {m} order by all").fetchall() for m, c in cols.items()}

    before = raw()
    con = build(lake, tmp_path)
    first, first_unobserved = marts(con), marts(con, observed=False)
    lake_mod.Lake()  # refreshes the local schema file only
    second = marts(build(lake, tmp_path))
    assert raw() == before and first == second  # raw untouched; same lake, same tables

    got = {r[0]: r[1:] for r in build(lake, tmp_path).execute(
        "select product_id, catalog_status, published_at from dim_coffee").fetchall()}  # fmt: skip
    assert got == {"A": ("available", pub), "B": ("delisted", None), "C": ("available", pub)}

    # Re-running the week adds a batch (never rewrites one) and changes nothing downstream.
    run(
        lake,
        "2026-10-26",
        rid,
        [product("A") | {"published_at": pub}, product("C") | {"published_at": pub}],
        hour=18,
    )
    assert set(before) < set(raw()) and all(raw()[k] == v for k, v in before.items())
    # The latest successful batch of a week wins (a forced re-fetch replaces bad data), so only the time its
    # events were observed moves; every coffee, status, price and event is the same.
    assert marts(build(lake, tmp_path), observed=False) == first_unobserved


def test_catalog_status_separates_current_from_back_catalog(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    tagged = lambda pid, tags, **kw: product(pid, **kw) | {"tags": tags}  # noqa: E731
    run(lake, "2026-10-19", rid, [product("GONE")])
    run(
        lake,
        "2026-10-26",
        rid,
        [
            product("NOW"),
            product("OUT", available=False),
            tagged("OLD", ["Archived"], available=False),
            tagged("OLD2", ["past-offering"], available=False),
            tagged("SOON", ["preorder-ship-date:2026-11-05"]),
            tagged(
                "BACK", ["archive"]
            ),  # archived, but buyable again: what matters now is that it's in stock
            tagged("PASTA", ["Pasta"], available=False),
            tagged("SLUG", ["past_offering"], available=False),
            tagged("SOON2", ["coming-soon"]),
            tagged("SHIPPED", ["preorder-ship-date:2026-10-01"]),  # ship date before this run: a leftover tag
        ],
    )
    con = build(lake, tmp_path)
    got = dict(con.execute("select product_id, catalog_status from dim_coffee").fetchall())
    assert got == {
        "GONE": "delisted",
        "NOW": "available",
        "OUT": "sold_out",
        "OLD": "archived",
        "OLD2": "archived",
        "SOON": "preorder",
        "BACK": "available",
        "PASTA": "sold_out",
        "SLUG": "archived",
        "SOON2": "preorder",
        "SHIPPED": "available",
    }
    # Counts describe the current catalog; the archived back catalog is counted on its own.
    listed, archived = con.execute(
        "select coffees_listed, coffees_archived from dim_roaster where roaster_id = ?", [rid]
    ).fetchone()
    assert (listed, archived) == (7, 3)  # 10 listed this run, less OLD, OLD2 and SLUG


def test_new_arrivals_are_newly_published_not_newly_fetched(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    run(lake, "2026-10-19", rid, [product("A")])
    old, new = datetime(2024, 3, 1, tzinfo=UTC), datetime(2026, 10, 22, tzinfo=UTC)
    run(lake, "2026-10-26", rid, [
        product("A"),
        product("BACKLIST") | {"published_at": old},  # first fetched now (pagination), published long ago
        product("FRESH") | {"published_at": new},  # published after the previous run
        product("UNKNOWN"),  # no publish time (not Shopify): still counts
    ])  # fmt: skip
    con = build(lake, tmp_path)
    assert sorted(r[0] for r in con.execute("select coffee_id from mart_new_arrivals").fetchall()) == [
        f"{rid}:FRESH", f"{rid}:UNKNOWN",
    ]  # fmt: skip


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


def test_python_and_dbt_agree_on_no_catalog_and_platformless_roasters(lake, tmp_path):
    """The status CASE's last branches (registry.STATE_SQL and int_roaster_state): an ok fetch with no products is
    no_catalog; a probe that answered with an HTTP error (platform http_<code>) or a roaster with no platform at all
    is unreachable, however its fetches went."""
    a, b, c = "onyxcoffeelab.com", "intelligentsia.com", "newroaster.example"
    run(lake, "2026-10-19", a, [])  # ok, nothing listed
    probe(lake, "2026-10-18", b, platform="http_404")
    run(lake, "2026-10-19", b, [product("1")])  # even with an ok fetch of coffee
    lake.write(
        "registry",
        "registry/source=overture/x.parquet",
        [
            {
                "added_at": datetime(2026, 10, 1, tzinfo=UTC),
                "source": "overture",
                "roaster_id": c,
                "name": "New",
            }
        ],
    )  # never probed: no platform
    con = build(lake, tmp_path)
    dbt_status = dict(con.execute("select roaster_id, status from dim_roaster").fetchall())
    py_status = {s.roaster.roaster_id: s.status for s in registry.current(lake, FIX / "roasters.csv")}
    assert dbt_status == py_status
    assert (py_status[a], py_status[b], py_status[c]) == ("no_catalog", "unreachable", "unreachable")


def test_one_store_is_one_roaster_in_python_dbt_and_the_public_tables(lake, tmp_path):
    """eval/roaster_dedup: a second domain whose site resolves to an active roaster's store is an alias, in the
    crawl's state (registry.current) and in dbt alike. Discovery adds intelligentsiacoffee.com, whose probe lands on
    www.Intelligentsia.com (any case): while it is only pending it is already the alias, so it is never crawled, and
    once both were read the id equal to the host stays canonical. The alias is in no public table."""
    from datetime import UTC, datetime

    from coffeedata import ingest

    main, alias = "intelligentsia.com", "intelligentsiacoffee.com"
    lake.write(
        "registry",
        "registry/source=overture/x.parquet",
        [{"added_at": datetime(2026, 11, 1, tzinfo=UTC), "source": "overture", "roaster_id": alias,
          "name": "Intelligentsia Coffee"}],
    )  # fmt: skip
    run(lake, "2026-10-26", main, [product("A", 20.0), product("B", 18.0)])
    probe(lake, "2026-11-01", alias, final_host="www.Intelligentsia.com")
    py = {s.roaster.roaster_id: s.status for s in registry.current(lake, FIX / "roasters.csv")}
    assert (py[main], py[alias]) == ("active", "alias")
    assert alias not in {s.roaster.roaster_id for s in ingest.select_states(lake, "2026-11-02")[0]}
    # Crawled anyway (as before this change), the same products under the second id: still one store.
    run(lake, "2026-11-02", alias, [product("A", 20.0), product("B", 18.0)])
    run(lake, "2026-11-02", main, [product("A", 20.0), product("B", 18.0)])
    con = build(lake, tmp_path)
    dbt = {
        r[0]: r[1:]
        for r in con.execute("select roaster_id, status, canonical_roaster_id from dim_roaster").fetchall()
    }
    py = {
        s.roaster.roaster_id: (s.status, s.canonical_roaster_id)
        for s in registry.current(lake, FIX / "roasters.csv")
    }
    assert dbt == py
    assert dbt[alias] == ("alias", main) and dbt[main] == ("active", main)
    assert [
        r[0]
        for r in con.execute(
            "select roaster_id from public_roaster where roaster_id like 'intell%'"
        ).fetchall()
    ] == [main]
    for t in ("public_coffee", "public_price_event"):
        assert con.execute(f"select count(*) from {t} where roaster_id = ?", [alias]).fetchone() == (0,), t
    assert con.execute("select count(*) from public_coffee where roaster_id = ?", [main]).fetchone() == (2,)


def test_a_store_keeps_its_alias_through_an_outage_a_week_without_coffee_and_an_opt_out(lake, tmp_path):
    """Review of #137 (H1, M7): grouping never reads the crawl status, so the canonical id going unreachable, then
    reading no coffee, then opting out never makes its alias a store of its own again. Each time the build passes
    (public_roasters_unique_by_host and roaster_aliases_resolve are error tests), the alias is never crawled or
    public, and Python's state agrees with dbt's on every roaster's status and canonical id."""
    from datetime import UTC, datetime

    from coffeedata import ingest

    main, alias = "intelligentsia.com", "intelligentsiacoffee.com"

    def registry_row(rid, **kw):
        lake.write(
            "registry",
            f"registry/source=overture/{rid}-{len(kw)}.parquet",
            [{"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": rid, "name": rid}
             | kw],
        )  # fmt: skip

    registry_row(alias)
    run(lake, "2026-10-12", main, [product("A", 20.0)])
    run(lake, "2026-10-12", alias, [product("A", 20.0)])  # both read before the change
    probe(lake, "2026-10-13", alias, final_host="www.intelligentsia.com")

    def check(expected_main_status):
        con = build(lake, tmp_path)
        dbt = {r[0]: r[1:] for r in con.execute("select roaster_id, status, canonical_roaster_id from dim_roaster")
               .fetchall()}  # fmt: skip
        py = {s.roaster.roaster_id: (s.status, s.canonical_roaster_id)
              for s in registry.current(lake, FIX / "roasters.csv")}  # fmt: skip
        assert dbt == py
        assert dbt[main] == (expected_main_status, main) and dbt[alias] == ("alias", main)
        assert con.execute(
            "select count(*) from public_roaster where roaster_id = ?", [alias]
        ).fetchone() == (0,)
        assert con.execute("select count(*) from dim_coffee where roaster_id = ?", [alias]).fetchone() == (0,)
        con.close()
        assert alias not in {s.roaster.roaster_id for s in ingest.select_states(lake, "2026-11-30")[0]}

    for d in ("2026-10-19", "2026-10-26", "2026-11-02"):
        run(lake, d, main, None)  # the canonical store is down for 3 runs
    check("unreachable")
    run(lake, "2026-11-09", main, [product("M", coffee=False)])  # back, with only a mug for sale
    check("no_coffee")
    registry_row(main, opted_out=True)  # the store opts out: its other domain stays out too
    check("opted_out")


def test_a_stranded_store_warns_and_two_stores_on_a_parked_host_are_two(lake, tmp_path):
    """Final review of #137. N1: a canonical id that stays unreachable while its alias still answers is a store
    nobody crawls, so stranded_alias_stores warns (the runbook's fix is alias_of). N2: two published stores whose
    domains both lapsed to a parked host (hugedomains.com, seed shared_hosts) are two stores, as the alias rule has
    them, so public_roasters_unique_by_host (error) passes."""
    import re
    from datetime import UTC, datetime

    main, alias = "intelligentsia.com", "intelligentsiacoffee.com"
    lake.write(
        "registry",
        "registry/source=overture/x.parquet",
        [
            {
                "added_at": datetime(2026, 10, 1, tzinfo=UTC),
                "source": "overture",
                "roaster_id": alias,
                "name": alias,
            }
        ],
    )
    run(lake, "2026-10-12", main, [product("A", 20.0)])
    for d in ("2026-10-19", "2026-10-26", "2026-11-02"):
        run(lake, d, main, None)  # gone for good
    probe(lake, "2026-11-03", alias, final_host="www.intelligentsia.com")  # the alias still answers
    for rid in ("counterculturecoffee.com", "anaheimcoffee.com"):
        run(lake, "2026-11-02", rid, [product("B", 18.0)])
        probe(lake, "2026-11-03", rid, final_host="www.hugedomains.com", platform="other")
    r = build(lake, tmp_path, check=False)
    assert r.returncode == 0, r.stdout[-3000:]
    assert re.search(r"WARN 1 stranded_alias_stores", r.stdout), r.stdout[-3000:]
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    assert con.execute(
        "select alias_id, canonical_id from main_dbt_test__audit.stranded_alias_stores"
    ).fetchall() == [(alias, main)]
    public = {r[0] for r in con.execute("select roaster_id from public_roaster").fetchall()}
    assert {"counterculturecoffee.com", "anaheimcoffee.com"} <= public
    con.close()


def test_roaster_status_reads_the_recorded_coffee_count_else_counts_it(lake, tmp_path):
    """Status is decided as on main, from the last ok fetch's coffee count. A fetch from before ELT keeps the count
    its crawl recorded, even where today's rules see no coffee (kaldi.com 2026-10-26: 3,459 recorded, while its
    first 5,000 products, cut at the page cap, are equipment parts; hikersbrewcoffee.com: 6). A fetch whose crawl
    recorded none (ELT) is counted with the crawl's rule, classify.is_coffee (int_fetch_coffees)."""
    # The fixture registry's roasters, standing in for: kaldi.com, hikersbrewcoffee.com, an ELT fetch with a
    # coffee, and an ELT fetch with only gear.
    KALDI, HIKER, ELT, GEAR = (
        "counterculturecoffee.com",
        "intelligentsia.com",
        "onyxcoffeelab.com",
        "anaheimcoffee.com",
    )
    gear = [product("P1", coffee=False), product("P2", coffee=False)]
    run(lake, "2026-10-26", KALDI, gear, coffees=3459)
    run(lake, "2026-10-28", KALDI, None)  # a later failure doesn't change the last ok fetch
    run(lake, "2026-10-26", HIKER, [product("1"), product("M", coffee=False)], coffees=6)
    run(lake, "2026-11-02", ELT, [product("1"), product("M", coffee=False)], coffees=None)
    run(lake, "2026-11-02", GEAR, gear, coffees=None, adapter="woocommerce")
    con = build(lake, tmp_path)
    dbt_status = dict(con.execute("select roaster_id, status from dim_roaster").fetchall())
    counted = dict(con.execute("select roaster_id, coffees from int_fetch_coffees").fetchall())
    con.close()
    py_status = {s.roaster.roaster_id: s.status for s in registry.current(lake, FIX / "roasters.csv")}
    want = {
        KALDI: "active",
        HIKER: "active",
        ELT: "active",
        GEAR: "no_coffee",
    }
    assert {k: dbt_status[k] for k in want} == want == {k: py_status[k] for k in want}
    assert counted == {ELT: 1, GEAR: 0}  # only the fetches without a recorded count


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
    # The licence text travels with the data, as LICENSE, once the repository has it (public.license_file()).
    assert (tmp_path / "public" / "LICENSE").is_file() == (public.license_file() is not None)
    assert sorted(p.name for p in (tmp_path / "public").iterdir() if p.name != "LICENSE") == [
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
    # One row per bag: its shots, the best one, and days off roast at the first and last shot.
    assert con.execute(
        "select user_id, bag_id, coffee_id, shot_count, best_shot_id, best_rating, days_off_roast_first,"
        " days_off_roast_last, shots_in_window from mart_bag"
    ).fetchall() == [("u1", "b1", f"{rid}:A", 2, "new1", 5, 8, 8, 2)]


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
    # Every file the site writes (landing, catalog, legal pages, icons): none carries a shot's data. (The landing
    # page does mention that shots can be logged through the MCP server.) Read as bytes: the icons are PNGs.
    text = "".join(
        p.read_bytes().decode(errors="replace") for p in out.parent.rglob("*") if p.is_file()
    ).lower()
    assert not any(w in text for w in ("secretshot", "private blend", "kitchen notes", "rated "))
    assert "rating by brew ratio" not in text and "recent shots" not in text


def test_site_has_a_launch_page_and_the_catalog_app(lake, tmp_path, monkeypatch):
    from coffeedata import site

    monkeypatch.setenv("PUBLIC_EXPORT_URL", "hf://datasets/someone/us-coffee-market")
    probe(lake, "2026-10-11", "onyxcoffeelab.com")
    run(lake, "2026-10-12", "onyxcoffeelab.com", [product("A")])
    evil = product("E") | {"url": "javascript:alert(1)"}
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A", price=22.0), product("B"), evil])
    con = build(lake, tmp_path)
    # The unsafe link is dropped in the model, so the public tables, exports and MCP server never see it.
    assert con.execute("select url from dim_coffee where title = 'Coffee E Roast'").fetchone() == (None,)
    assert con.execute(
        "select count(*) from public_coffee where coffee_name = 'Coffee E Roast'"
    ).fetchone() == (0,)
    con.close()

    out = tmp_path / "site"
    site.build(tmp_path / "coffee.duckdb", out)
    landing = (out / "index.html").read_text()
    assert 'href="catalog/"' in landing
    assert "https://huggingface.co/datasets/someone/us-coffee-market" in landing
    assert "hf://datasets/someone/us-coffee-market/data/public_coffee.parquet" in landing
    assert "Coffee B Roast" in landing  # a new arrival
    assert 'href="../"' in (out / "catalog" / "index.html").read_text()
    # The favicon on every page, served from the site root; the old inline bean is gone.
    for page in ("", "catalog/", "privacy/", "terms/", "bot/"):
        root = "../" if page else ""
        html = (out / page / "index.html").read_text()
        for icon in ("favicon.svg", "favicon-64.png", "apple-touch-icon.png"):
            assert f'href="{root}{icon}"' in html
        assert "data:image/svg+xml" not in html
    for f in ("favicon.svg", "favicon-64.png", "apple-touch-icon.png", "mark-1024.png"):
        assert (out / f).read_bytes() == (site.STATIC / f).read_bytes()
    # The app's beta: sign in and request an invite from the launch page and the catalog; requests are an email,
    # since the static site collects nothing.
    for html in (landing, (out / "catalog" / "index.html").read_text()):
        assert 'href="https://app.registry.coffee/signin"' in html
        assert 'href="mailto:support@registry.coffee?subject=registry.coffee%20beta%20invite"' in html
        assert "<form" not in html
        assert "Nothing you log appears on this site." in html  # and no promise beyond that
    assert '<a class="mark-word serif" href="./" aria-current="page">' in landing
    # "Updated" is the last fetch, not the build time, so a template-only redeploy doesn't freshen it.
    assert "Updated 2026-10-19 12:00 UTC" in (out / "catalog" / "index.html").read_text()
    for page, heading in (("privacy", "Privacy Policy"), ("terms", "Terms of Use")):
        html = (out / page / "index.html").read_text()
        assert f'<h1 class="serif">{heading}</h1>' in html and f"Last updated {site.LEGAL_UPDATED}" in html
        assert "Content-Security-Policy" in html and "support@registry.coffee" in html
        assert f'href="{page}/"' in landing
        assert f'href="../{page}/"' in (out / "catalog" / "index.html").read_text()
    assert "State of Michigan" in (out / "terms" / "index.html").read_text()
    # /bot, the page the crawler's User-Agent links to: who we are, what and when we crawl, how to opt out, contact
    bot = (out / "bot" / "index.html").read_text()
    assert '<h1 class="serif">RegistryCoffeeBot</h1>' in bot and "Content-Security-Policy" in bot
    assert "RegistryCoffeeBot/1.0 (+https://registry.coffee/bot; support@registry.coffee)" in bot
    assert "on Mondays" in bot and "Crawl-delay" in bot and "up to 60 seconds" in bot and "Retry-After" in bot
    assert "User-agent: RegistryCoffeeBot\nDisallow: /" in bot
    # opt-out is robots.txt or email: the issue form only acts on the owner's own issues (issue-forms.yml)
    assert "add-roaster.yml" not in bot and "at least 1 second apart" in bot and "platform" in bot
    assert 'href="mailto:support@registry.coffee"' in bot
    assert 'href="bot/"' in landing and 'href="../bot/"' in (out / "privacy" / "index.html").read_text()
    # sitemap.xml lists every page the site writes
    written = sorted(str(p.parent.relative_to(out)) for p in out.rglob("index.html"))
    assert written == sorted((page.rstrip("/") or ".") for page in site.PAGES)
    catalog = json.loads((out / "catalog" / "catalog.json").read_text())
    assert catalog["rows"]
    app = (out / "catalog" / "index.html").read_text()
    for html in (landing, app):
        assert "javascript:" not in html
        assert "Content-Security-Policy" in html and "script-src 'sha256-" in html
    assert None in [r[catalog["columns"].index("url")] for r in catalog["rows"]]  # the javascript: link


def test_site_shows_only_the_public_roasters(lake, tmp_path):
    """The site lists the same roasters as public_roaster: a roaster known only from OpenStreetMap (ODbL) or opted
    out appears nowhere on it, not in the catalog JSON, the new arrivals, the charts or the counts."""
    from coffeedata import site

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
    for rid, title in (
        ("onyxcoffeelab.com", "Geometry Ethiopia"),
        ("osmonly.coffee", "Odbl Secret Kenya"),
        ("vividcoffee.com", "Optedout Ethiopia"),
    ):
        probe(lake, "2026-10-18", rid)
        run(lake, "2026-10-19", rid, [product("A", title=title)])
        run(lake, "2026-10-26", rid, [product("A", 22.0, title=title), product("N", title=f"{title} New")])
    con = build(lake, tmp_path, build_id="20261027T060000-abcdef", as_of_run="2026-10-26", **ANY_RUN)
    # The OSM-only roaster's coffees are in dim_coffee (the private lake); only public_roaster leaves it out.
    assert (
        con.execute("select count(*) from dim_coffee where roaster_id = 'osmonly.coffee'").fetchone()[0] == 2
    )
    assert {r[0] for r in con.execute("select roaster_id from public_roaster").fetchall()} == {
        "onyxcoffeelab.com"
    }
    con.close()

    out = tmp_path / "site"
    site.build(tmp_path / "coffee.duckdb", out)
    catalog = json.loads((out / "catalog" / "catalog.json").read_text())
    roasters = {r[catalog["columns"].index("coffee_id")].split(":")[0] for r in catalog["rows"]}
    assert roasters == {"onyxcoffeelab.com"}
    # Read as bytes: the icons are PNGs.
    text = "".join(p.read_bytes().decode(errors="replace") for p in out.rglob("*") if p.is_file()).lower()
    assert "geometry ethiopia new" in text  # onyx's new arrival is there
    for word in ("osmonly", "osm only", "odbl secret", "vividcoffee", "optedout"):
        assert word not in text, word
    # The release this site shows, in the catalog's header and the Dataset JSON-LD.
    assert catalog["release"] == {
        "release_build_id": "20261027T060000-abcdef",
        "release_run_date": "2026-10-26",
        "contract": f"coffee-data-public-catalog@{public_contract_version()}",
        "license": "CDLA-Permissive-2.0",
        "license_url": "https://github.com/jtouley/coffee-data-tracker/blob/main/licenses/CDLA-Permissive-2.0.txt",
    }
    assert list(catalog) == ["release", "columns", "rows", "roasters"]
    landing = (out / "index.html").read_text()
    assert '"version":"20261027T060000-abcdef"' in landing and '"dateModified":"2026-10-26"' in landing


def test_site_catalog_names_each_roaster_once_with_its_place(lake, tmp_path):
    """catalog.json lists each roaster once (id, name, city and state as stored, from public_roaster); a coffee's
    `roaster` is that roaster's row number, so the name and place aren't repeated on every coffee. The page's script
    reads both."""
    from coffeedata import site

    for rid in ("onyxcoffeelab.com", "1802roasters.com"):
        run(lake, "2026-10-19", rid, [product("A"), product("B")])
    build(lake, tmp_path).close()
    out = tmp_path / "site"
    site.build(tmp_path / "coffee.duckdb", out)
    catalog = json.loads((out / "catalog" / "catalog.json").read_text())
    assert catalog["roasters"] == {
        "columns": ["roaster_id", "roaster_name", "city", "state"],
        # The fixture registry has no city for 1802 Roasters: left empty, not guessed.
        "rows": [["1802roasters.com", "1802 Roasters", None, "CA"], ["onyxcoffeelab.com", "Onyx Coffee Lab", "Rogers", "AR"]],
    }  # fmt: skip
    cols = catalog["columns"]
    assert "roaster" in cols and not {"roaster_id", "roaster_name", "roaster_state"} & set(cols)
    assert sorted(
        (catalog["roasters"]["rows"][r[cols.index("roaster")]][0], r[cols.index("coffee_id")])
        for r in catalog["rows"]
    ) == sorted((rid, f"{rid}:{p}") for rid in ("onyxcoffeelab.com", "1802roasters.com") for p in "AB")
    page = (out / "catalog" / "index.html").read_text()
    assert '<select id="state" aria-label="Roaster state (number of roasters)">' in page
    assert '<p class="note" id="count" role="status">' in page
    assert '"MI": "Michigan"' in page and '["west virginia", "WV"]' in page


# A release's vars without the run checks (the fixture registry lists roasters these lakes never fetch).
ANY_RUN = {"min_attempted_share": 0, "min_ok_share": 0}


def public_contract_version() -> str:
    import yaml

    return str(yaml.safe_load((ROOT / "contracts" / "public-catalog.odcs.yaml").read_text())["version"])


def test_public_export_describes_its_release(lake, tmp_path):
    """manifest.json names the release, contract and licence, and each file's sha256 and row count."""
    import hashlib

    from coffeedata import public

    run(lake, "2026-10-26", "onyxcoffeelab.com", [product("A"), product("B", 18.0)])
    build(lake, tmp_path, build_id="20261027T060000-abcdef", as_of_run="2026-10-26", **ANY_RUN).close()
    out = public.export(tmp_path / "coffee.duckdb", str(tmp_path / "public"))
    manifest = json.loads((tmp_path / "public" / "manifest.json").read_text())
    assert manifest["release_build_id"] == out["release_build_id"] == "20261027T060000-abcdef"
    assert manifest["release_run_date"] == "2026-10-26" and manifest["as_of_run"] == "2026-10-26"
    assert manifest["contract"] == {"id": "coffee-data-public-catalog", "version": public_contract_version()}
    assert manifest["license"]["id"] == "CDLA-Permissive-2.0" and manifest["license"]["url"].endswith(
        "/licenses/CDLA-Permissive-2.0.txt"
    )
    assert sorted(manifest["files"]) == sorted(f"{t}.parquet" for t in public.PUBLIC_TABLES)
    for name, f in manifest["files"].items():
        data = (tmp_path / "public" / name).read_bytes()
        assert f["sha256"] == hashlib.sha256(data).hexdigest()
        assert f["rows"] == manifest["tables"][name.removesuffix(".parquet")]
        assert (
            f["rows"]
            == duckdb.connect().execute(f"select count(*) from '{tmp_path / 'public' / name}'").fetchone()[0]
        )
    assert manifest["files"]["public_coffee.parquet"]["rows"] == 2


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://onyxcoffeelab.com/products/geometry", True),
        ("http://x.coffee/p?id=1", True),
        ("javascript:alert(1)", False),
        ("JAVASCRIPT:alert(1)", False),
        ("data:text/html,hi", False),
        ('https://x.com/"><script>', False),
        ("https://x.com/a\n", False),
        ("https://x.com/farmer's-reserve", True),
        (None, False),
    ],
)
def test_safe_url(url, ok):
    from coffeedata import site

    assert site.safe_url(url) == (url if ok else None)


def test_site_links_without_a_public_hf_dataset(monkeypatch):
    from coffeedata import site

    assert site.links("s3://bucket/coffee", "https://example.org/")["dataset"] is None
    assert site.links("hf://datasets/a/b@main")["dataset"] == "https://huggingface.co/datasets/a/b"
    monkeypatch.setenv("APP_URL", "https://app.staging.example/")
    assert site.links()["signin"] == "https://app.staging.example/signin"


def _landing(**reach) -> str:
    from jinja2 import Environment, PackageLoader, select_autoescape

    from coffeedata import site

    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    return env.get_template("landing.html").render(
        totals={"coffees": 1, "in_stock": 1, "roasters": 1},
        registry=1,
        reach={"origins": 1, "states": 1, "has_dc": False, "has_pr": False} | reach,
        week={"new": 0, "price_changes": 0, "removed": 0},
        new=[],
        charts={"origins": ""},
        links=site.links("", None),
        public_tables=4,
        generated="today",
    )


@pytest.mark.parametrize(
    "states, has_dc, has_pr, text",
    [
        (50, True, True, "across 50 states, DC and Puerto Rico."),
        (50, True, False, "across 50 states and DC."),
        (50, False, False, "across 50 states."),
        (1, False, False, "across 1 state."),
    ],
)
def test_landing_counts_dc_and_puerto_rico_apart_from_the_states(states, has_dc, has_pr, text):
    assert text in _landing(states=states, has_dc=has_dc, has_pr=has_pr)


def test_landing_hides_the_query_section_without_a_hugging_face_export():
    # No PUBLIC_EXPORT_URL (or one that isn't hf://): no path to query, so no "Query it from anywhere" and no
    # placeholder path a reader could copy.
    html = _landing()
    assert "Query it from anywhere" not in html and "hf://" not in html and "&lt;owner&gt;" not in html


def test_csp_needs_somewhere_to_go():
    from coffeedata import site

    assert "Content-Security-Policy" in site.with_csp('<head><meta charset="utf-8"></head><script>x</script>')
    with pytest.raises(ValueError):
        site.with_csp("<head><meta charset=utf-8></head>")


def test_site_icons_are_same_origin_under_the_csp(monkeypatch):
    """The favicon and the link-preview image are files on the site, so img-src 'self' covers them: no data: icon,
    nothing third-party, and the CSP itself is unchanged."""
    import re

    from coffeedata import site

    monkeypatch.delenv("SITE_URL", raising=False)
    html = site.with_csp(_landing())
    assert "img-src 'self' data:;" in html
    icons = re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="([^"]+)"', html)
    assert icons == ["favicon-64.png", "favicon.svg", "apple-touch-icon.png"]
    assert all((site.STATIC / i).is_file() for i in icons)
    # og:image must be absolute for link previews; it's the site's own file.
    assert '<meta property="og:image" content="https://registry.coffee/mark-1024.png">' in html
    assert (site.STATIC / "favicon.svg").read_text().startswith('<svg xmlns="http://www.w3.org/2000/svg"')


@pytest.mark.parametrize(
    "url",
    ["hf://datasets/a/b", "hf://datasets/a/b@main", "hf://datasets/a/b/data", "hf://datasets/a/b/"],
)
def test_hf_repo_id(url):
    from coffeedata import public

    assert public.hf_repo_id(url) == "a/b"


def test_every_roaster_status_has_a_coverage_label():
    """A status added to dim_roaster without a label would silently drop out of the coverage table."""
    import re

    from coffeedata import site

    # dim_roaster's status is decided in int_roaster_state
    sql = (ROOT / "dbt" / "models" / "intermediate" / "int_roaster_state.sql").read_text()
    statuses = set(re.findall(r"(?:then|else) '(\w+)'", sql))
    assert statuses and statuses == set(site.STATUS_LABELS)


def test_states_are_the_fifty():
    from coffeedata import site

    assert len(site.STATES) == 50 and "DC" not in site.STATES and "PR" not in site.STATES


def test_dbt_unit_tests(lake, tmp_path):
    """dbt's unit tests (dbt/models/unit_tests.yml): the suspect-run rule, each kind of price event, tag decoding, the
    partial and challenged runs the fetch-health check reads, the dial-in and verdict tie rules, one canonical roaster
    per store. They read hand-written rows, but dbt reads the column types of their inputs from built tables, so a
    small lake is built first."""
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A")])
    build(lake, tmp_path).close()
    r = run_dbt(lake, tmp_path, "test", "--select", "test_type:unit", "--exclude", "tag:suspected_bug")
    assert r.returncode == 0, r.stdout[-4000:]
    assert "PASS=22 " in r.stdout


def test_public_files_are_written_in_key_order(lake, tmp_path):
    from coffeedata import public

    for rid in ("onyxcoffeelab.com", "counterculturecoffee.com"):
        run(lake, "2026-10-19", rid, [product("B", 18.0), product("A"), product("C")])
        run(lake, "2026-10-26", rid, [product("C", 25.0), product("A", 22.0), product("D")])
    build(lake, tmp_path).close()
    first = public.export(tmp_path / "coffee.duckdb", str(tmp_path / "one"))
    public.export(tmp_path / "coffee.duckdb", str(tmp_path / "two"))
    for t in public.PUBLIC_TABLES:
        a, b = (tmp_path / d / f"{t}.parquet" for d in ("one", "two"))
        assert a.read_bytes() == b.read_bytes(), t  # the same release, the same file
    path = tmp_path / "one" / "public_price_event.parquet"
    cur = duckdb.connect().execute(f"select run_date, coffee_id, variant_id, event from '{path}'")
    events = cur.fetchall()
    assert len(events) == first["tables"]["public_price_event"] > 4 and events == sorted(events)


def _rows(con, table):
    return con.execute(f"select * from {table} order by all").fetchall()


def test_the_same_lake_always_builds_the_same_tables(lake, tmp_path):
    """Idempotent builds: nothing in the marts depends on when or how often the build runs."""
    probe(lake, "2026-10-18", "onyxcoffeelab.com")
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A"), product("B", 18.0)])
    run(lake, "2026-10-26", "onyxcoffeelab.com", [product("A", 22.0)])
    first = build(lake, tmp_path / "one")
    second = build(lake, tmp_path / "two")
    for table in ("dim_coffee", "fct_price_event", "mart_market", "public_coffee", "public_price_event"):
        assert _rows(first, table) == _rows(second, table), table


def test_classification_comes_from_the_current_rules_not_the_stored_flag(lake, tmp_path):
    """A rules fix reaches every past week: the flag and attributes stored at fetch time are ignored."""
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    part = product(
        "P", title="La Marzocco (T.1.054) Tube, Valve To Discharge | Fits Mistral"
    )  # stored: coffee
    bean = product("K", coffee=False, title="Kenya Kamwangi AA Washed")  # stored: not coffee
    mocha = product("M", title="Mocha Java Blend")
    run(lake, "2026-10-19", rid, [part, bean, mocha])
    # An enrichment row from older rules said Mocha Java was from Indonesia.
    lake.write_partition(
        "enrichments",
        "2026-10-19",
        "old",
        [
            {
                "extracted_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
                "roaster_id": rid,
                "product_id": "M",
                "body_hash": "bM",
                "method": "rules",
                "needs_llm": False,
                "origin_country": "Indonesia",
                "is_blend": False,
            }
        ],
    )
    con = build(lake, tmp_path)
    got = dict(con.execute("select product_id, origin_country from dim_coffee").fetchall())
    assert got == {"K": "Kenya", "M": None}
    events = {r[0] for r in con.execute("select coffee_id from fct_price_event").fetchall()}
    assert events == {f"{rid}:K", f"{rid}:M"}
    assert con.execute("select is_blend from dim_coffee where product_id = 'M'").fetchone() == (True,)


def test_claudes_extraction_still_overrides_the_rules(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    run(lake, "2026-10-19", rid, [product("A", title="House Espresso Roast")])
    lake.write_partition(
        "enrichments",
        "2026-10-19",
        "llm1",
        [
            {
                "extracted_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
                "roaster_id": rid,
                "product_id": "A",
                "body_hash": "bA",
                "method": "llm",
                "needs_llm": False,
                "origin_country": "Brazil",
                "process": "natural",
                "is_blend": False,
            }
        ],
    )
    con = build(lake, tmp_path)
    assert con.execute("select origin_country, process, enrichment_method from dim_coffee").fetchone() == (
        "Brazil",
        "natural",
        "llm",
    )


def test_wholesale_twins_stay_in_the_data_but_out_of_what_people_browse(lake, tmp_path, monkeypatch):
    from coffeedata import mcp_server, shots, site

    rid = "onyxcoffeelab.com"  # in the test seed list, so its coffees reach the public tables
    run(lake, "2026-10-19", rid, [product("OLD", title="Colombia Supremo")])
    run(lake, "2026-10-26", rid, [
        product("OLD", title="Colombia Supremo"),
        product("GUJI", title="Ethiopia Guji"),
        product("WGUJI", title="Wholesale - 5LB Ethiopia Guji"),  # the same coffee, sold to cafes
    ])  # fmt: skip
    con = build(lake, tmp_path)
    wholesale = dict(
        con.execute("select split_part(coffee_id, ':', 2), is_wholesale from public_coffee").fetchall()
    )
    assert wholesale == {"OLD": False, "GUJI": False, "WGUJI": True}  # kept, flagged
    assert [r[0] for r in con.execute("select coffee_id from mart_new_arrivals").fetchall()] == [
        f"{rid}:GUJI"
    ]
    assert (
        con.execute("select sum(coffees) from mart_market where origin_country = 'Ethiopia'").fetchone()[0]
        == 1
    )
    assert (
        con.execute("select coffees_listed from dim_roaster where roaster_id = ?", [rid]).fetchone()[0] == 2
    )
    con.close()

    site.build(tmp_path / "coffee.duckdb", tmp_path / "site")
    catalog = json.loads((tmp_path / "site" / "catalog" / "catalog.json").read_text())
    titles = [r[catalog["columns"].index("title")] for r in catalog["rows"]]
    assert "Wholesale - 5LB Ethiopia Guji" not in titles and "Ethiopia Guji" in titles

    # The app's picker and Claude's search read the exported mart; neither offers the wholesale twin by default.
    mart = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    lake.write_mart("dim_coffee", mart, "select * from dim_coffee")
    mart.close()
    shots._catalog_cache.clear()
    retail = sorted(t for _, _, t, is_retail, _ in shots._catalog(lake) if is_retail)
    assert retail == ["Colombia Supremo", "Ethiopia Guji"]
    # Matching still sees the twin, so a bag that says wholesale gets the wholesale coffee_id (review #3). Any
    # other bag is decided among retail coffees: the twin ties with its retail coffee, and mustn't make it unclear.
    assert shots.match_bag(None, "Wholesale - 5LB Ethiopia Guji", lake)[0] == f"{rid}:WGUJI"
    coffee_id, offered = shots.match_bag(None, "Ethiopia Guji", lake)
    assert coffee_id == f"{rid}:GUJI" and f"{rid}:WGUJI" not in {m["coffee_id"] for m in offered}
    assert shots.match_coffee("Onyx Ethiopia Guji", lake)[0] == f"{rid}:GUJI"
    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))
    search = getattr(mcp_server.search_coffees, "fn", mcp_server.search_coffees)
    assert sorted(r["title"] for r in search()) == ["Colombia Supremo", "Ethiopia Guji"]
    assert {r["title"]: r["is_wholesale"] for r in search(include_wholesale=True)} == {
        "Colombia Supremo": False,
        "Ethiopia Guji": False,
        "Wholesale - 5LB Ethiopia Guji": True,
    }


def test_wholesale_twins_count_in_no_weekly_or_roaster_number(lake, tmp_path):
    # Review #2 and #5: the week's price changes and removals, and the archived count, leave wholesale out too
    # (and only wholesale: an event of a coffee dim_coffee no longer keeps still counts).
    from coffeedata import site

    rid = "onyxcoffeelab.com"
    tagged = lambda pid, tags, **kw: product(pid, **kw) | {"tags": tags}  # noqa: E731
    run(lake, "2026-10-19", rid, [
        product("GUJI", title="Ethiopia Guji"),
        product("WGUJI", title="Wholesale - 5LB Ethiopia Guji"),
        product("WKEN", title="Wholesale - 5LB Kenya Kiambu"),
        product("GONE"),
    ])  # fmt: skip
    run(lake, "2026-10-26", rid, [  # WKEN is gone
        product("GONE", coffee=False),  # no longer coffee: out of dim_coffee, but its removal still counts
        product("GUJI", price=22.0, title="Ethiopia Guji"),
        product("WGUJI", price=99.0, title="Wholesale - 5LB Ethiopia Guji"),  # repriced
        tagged("OLD", ["Archived"], available=False, title="Colombia Supremo"),
        tagged("WOLD", ["Archived"], available=False, title="Wholesale - 5LB Colombia Supremo"),
    ])  # fmt: skip
    con = build(lake, tmp_path)
    [archived] = con.execute(
        "select coffees_archived from dim_roaster where roaster_id = ?", [rid]
    ).fetchone()
    assert archived == 1  # OLD; its wholesale twin WOLD isn't counted
    con.close()
    site.build(tmp_path / "coffee.duckdb", tmp_path / "site")
    landing = " ".join((tmp_path / "site" / "index.html").read_text().split())
    assert "<b>1</b> price changes · <b>1</b> delisted" in landing


def test_a_remote_root_is_read_only_for_folders_it_has(lake, tmp_path):
    """A table added after a Hugging Face lake was made has no folder there until a publish sends its schema file,
    and DuckDB fails on a glob over it. With remote_folders listing none, the build never touches the remote."""
    r = build(lake, tmp_path, check=False, lake_roots=[str(lake.root), "hf://datasets/nobody/missing"],
              remote_folders=[])  # fmt: skip
    assert r.returncode == 0, r.stdout[-3000:]


def test_tags_are_decoded_in_the_build_and_kept_raw_in_the_lake(lake, tmp_path):
    """Some stores' APIs escape tags (WooCommerce term names: "Parts &amp; Equipment"). The build decodes them,
    so the classifier reads the tag the store shows; the lake keeps the raw value, and the product-version key
    (input_hash) stays over it, so stored verdicts and enrichments still join. These escapes decide nothing
    differently, so the Python readers of raw tags agree with the build and it passes."""
    from coffeedata import products

    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    mug = product("M", title="Logo Mug", coffee=False) | {"tags": ["Parts &amp; Equipment"]}
    bean = product("K", title="Kenya Kamwangi AA Washed") | {"tags": ["Coffee &gt; Single Origin", "Kenya"]}
    run(lake, "2026-10-19", rid, [mug, bean], coffees=None)
    con = build(lake, tmp_path)
    tags = dict(con.execute("select product_id, tags from stg_snapshots").fetchall())
    assert tags == {"M": ["Parts & Equipment"], "K": ["Coffee > Single Origin", "Kenya"]}
    assert con.execute("select product_id from dim_coffee").fetchall() == [("K",)]
    assert con.execute("select tags from int_fetch_product_inputs order by product_id").fetchall() == [
        (["Coffee > Single Origin", "Kenya"],),
        (["Parts & Equipment"],),
    ]
    assert con.execute("select coffees from int_fetch_coffees").fetchall() == [(1,)]
    assert con.execute("select count(*) from int_escaped_tag_inputs").fetchone() == (2,)
    assert con.execute("select count(*) from int_tag_decoding_parity").fetchone() == (0,)
    keys = dict(con.execute("select product_id, input_hash from stg_snapshots").fetchall())
    con.close()
    raw = duckdb.connect().execute(
        f"select product_id, tags, {products.input_hash_sql('s')} from read_parquet('{lake.root}/snapshots/*/*.parquet') s"
    )
    got = {pid: (t, h) for pid, t, h in raw.fetchall()}
    assert got == {
        "M": (["Parts &amp; Equipment"], keys["M"]),
        "K": (["Coffee &gt; Single Origin", "Kenya"], keys["K"]),
    }


def test_the_build_fails_when_raw_and_decoded_tags_decide_differently(lake, tmp_path):
    """registry.py, enrich.py and verdict.py read raw tags; the build reads decoded ones. A tag whose decoding
    flips a rule (here "Parts&nbsp;", a gear tag only once decoded) makes them disagree, and
    python_readers_agree_on_tags fails the build, naming the product. The models themselves still build."""
    from coffeedata import quality

    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    part = product("P", title="Coffee P Roast") | {"tags": ["Parts&nbsp;"]}
    bean = product("K", title="Kenya Kamwangi AA Washed") | {"tags": ["Coffee &gt; Single Origin"]}
    run(lake, "2026-10-19", rid, [part, bean], coffees=None)
    r = build(lake, tmp_path, check=False)
    assert r.returncode != 0
    failed = re.findall(r"in test (\w+) \(", r.stdout)
    assert failed == ["python_readers_agree_on_tags"], r.stdout[-4000:]
    assert "Got 1 result" in r.stdout and " ERROR=1 " in r.stdout
    db = tmp_path / "coffee.duckdb"
    con = duckdb.connect(str(db), read_only=True)
    # The build decides with the decoded tag: P is a part, not a coffee; the raw tag would make it one.
    assert con.execute("select product_id from dim_coffee").fetchall() == [("K",)]
    assert con.execute("select coffees from int_fetch_coffees").fetchall() == [(1,)]
    rows = con.execute("select product_id, raw_tags, tags, differs from int_tag_decoding_parity").fetchall()
    con.close()
    every = ["is_coffee", "excluded", "rules", "rules_with_coffee_verdict"]
    assert rows == [("P", ["Parts&nbsp;"], ["Parts\u00a0"], every)]
    head, example = quality.tag_parity_messages(db)
    assert head.startswith("1 products decide differently on raw and decoded tags")
    assert example.startswith(f"{rid}:P 'Coffee P Roast': {', '.join(sorted(every))} differ")


# Each error-severity guard that no input can trip through the models (its model's own filter already keeps such rows
# out), with the bad row the guard exists to stop, written into the built table it reads.
GUARD_BREAKS = {
    "price_per_100g_in_range": "update dim_coffee set price_per_100g = 250 where product_id = 'A'",
    "sane_coffee_listings": "update dim_coffee set title = 'Replacement Burrs' where product_id = 'A'",
    "unique_runs": "insert into stg_runs select * from stg_runs limit 1",
    "public_tables_are_facts_only": "alter table public_coffee add column tags varchar[]",
    # a latest version with no bag-weight row (#136): dim_coffee would silently keep its stored grams
    "variant_grams_are_valid": "delete from stg_variant_grams",
}


def test_each_pass_path_guard_fails_on_the_row_it_guards_against(lake, tmp_path):
    """price_per_100g_in_range, sane_coffee_listings, unique_runs, public_tables_are_facts_only and
    variant_grams_are_valid pass on every synthetic build, since the models never produce such a row. Each is made to fail here, so a guard that stopped
    selecting anything (a renamed column, a dropped ref) would show."""
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    run(lake, "2026-10-19", rid, [product("A"), product("B", 18.0)])
    build(lake, tmp_path).close()
    db = tmp_path / "coffee.duckdb"
    clean = tmp_path / "clean.duckdb"
    shutil.copy(db, clean)
    for guard, break_it in GUARD_BREAKS.items():
        ok = run_dbt(lake, tmp_path, "test", "--select", guard)
        assert ok.returncode == 0 and "PASS=1 " in ok.stdout, (guard, ok.stdout[-2000:])
        with duckdb.connect(str(db)) as con:
            con.execute(break_it)
        bad = run_dbt(lake, tmp_path, "test", "--select", guard)
        assert bad.returncode != 0 and "ERROR=1 " in bad.stdout, (guard, bad.stdout[-2000:])
        shutil.copy(clean, db)


def _build_with_a_chain(lake, tmp_path):
    """onyxcoffeelab.com and intelligentsia.com each list one Kenya; the seed marks intelligentsia.com a chain."""
    import csv

    config = tmp_path / "config"
    config.mkdir()
    with (FIX / "roasters.csv").open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["is_chain"] = "true" if r["roaster_id"] == "intelligentsia.com" else r["is_chain"]
    with (config / "roasters.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for rid, title in (("onyxcoffeelab.com", "Kenya Kiambu"), ("intelligentsia.com", "Kenya Gichathaini")):
        probe(lake, "2026-10-18", rid)
        run(lake, "2026-10-19", rid, [product("A", title=title)])
    return build(lake, tmp_path, config_dir=str(config), build_id="20261020T060000-abcdef", as_of_run="2026-10-19",
                 **ANY_RUN)  # fmt: skip


def test_a_chains_coffees_leave_the_market_and_the_site_catalog(lake, tmp_path):
    """is_chain (the seed's flag): mart_market and the site's catalog, origin and reach figures leave a chain's coffees
    out."""
    from coffeedata import site

    con = _build_with_a_chain(lake, tmp_path)
    assert dict(con.execute("select roaster_id, is_chain from dim_coffee").fetchall()) == {
        "onyxcoffeelab.com": False, "intelligentsia.com": True}  # fmt: skip
    assert con.execute(
        "select coffees, roasters from mart_market where origin_country = 'Kenya'"
    ).fetchall() == [(1, 1)]
    con.close()
    out = tmp_path / "site"
    site.build(tmp_path / "coffee.duckdb", out)
    catalog = json.loads((out / "catalog" / "catalog.json").read_text())
    assert {r[catalog["columns"].index("coffee_id")].split(":")[0] for r in catalog["rows"]} == {
        "onyxcoffeelab.com"
    }


# --- Suspected bugs (docs/RULES.md): what the models should output. Strict xfails: each fails once fixed. ---------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="suspected bug: public_market_weekly reads public_coffee, which has no is_chain, so it counts a chain's "
    "coffees that mart_market and the site leave out (today the Kenya cell is 2 coffees from 2 roasters)",
)
def test_public_market_weekly_leaves_out_chains(lake, tmp_path):
    con = _build_with_a_chain(lake, tmp_path)
    got = con.execute(
        "select coffees, roasters from public_market_weekly where origin_country = 'Kenya'"
    ).fetchall()
    con.close()
    assert got == [(1, 1)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="suspected bugs: the dbt unit tests tagged suspected_bug fail today (flavor_lookup_ignores_case: a "
    "capitalised note gets no category; a_bag_with_no_shots_has_none_in_window: null, not 0)",
)
@pytest.mark.parametrize(
    "unit_test", ["flavor_lookup_ignores_case", "a_bag_with_no_shots_has_none_in_window"]
)
def test_suspected_bug_unit_tests(lake, tmp_path, unit_test):
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A")])
    build(lake, tmp_path).close()  # dbt reads the inputs' column types from built tables
    r = run_dbt(lake, tmp_path, "test", "--select", unit_test)
    # Only the expected failure is an xfail; anything else raises RuntimeError, which fails the test:
    # - the named unit test must have been selected and run (renamed, deleted or disabled, dbt runs nothing and
    #   exits 0, and the bug would go untracked);
    # - a failure must be its output differing from the expected one, not a broken test.
    if "TOTAL=1" not in r.stdout or f"::{unit_test}" not in r.stdout:
        raise RuntimeError(f"{unit_test} wasn't selected and run:\n{r.stdout[-2000:]}")
    if r.returncode and "differs from" not in r.stdout:
        raise RuntimeError(r.stdout[-2000:])
    assert r.returncode == 0 and "PASS=1 " in r.stdout, r.stdout[-2000:]


def test_bag_weights_are_read_again_from_the_stored_text(lake, tmp_path):
    """stg_variant_grams (coffeedata.bag_weight) gives dim_coffee its grams: a weight in the product title, a
    slug-style option or the description fills options the crawler stored none for; a misread option ("1/2 lb" stored
    as 907 g) is corrected; a pack with a count is its total; a bundle's weight is dropped and not refilled from the
    title; a stored weight the listing never states (Shopify's grams) is dropped. The raw snapshots keep what the
    crawler stored."""
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)

    def opts(p, *variants):
        p["variants"] = [
            {
                "variant_id": f"{p['product_id']}-{i}",
                "title": t,
                "price_usd": price,
                "grams": g,
                "available": True,
            }
            for i, (t, price, g) in enumerate(variants)
        ]
        return p

    products = [
        opts(product("A", title="Bolivia Coffee - 1 lb"), ("Whole Bean", 20.0, None), ("Drip", 20.0, None)),
        opts(
            product("B", title="Colombia Coffee"),
            ("1-lb-whole-bean", 18.0, None),
            ("5-lb-whole-bean", 80.0, None),
        ),
        opts(product("C", title="Mexico Coffee"), ("1/2 lb", 10.0, 907), ("1 lb", 18.0, 454)),
        opts(product("D", title="Kenya Coffee"), ("Whole Bean", 17.0, None)),
        opts(product("E", title="Peru Coffee"), ("12oz - 3 Pack", 45.0, 340)),
        opts(product("F", title="Sumatra Coffee 12 oz"), ("12 oz Bundle", 30.0, 340)),
        opts(product("G", title="Ethiopia Coffee"), ("Default Title", 19.0, 363)),
    ]
    run(lake, "2026-10-19", rid, products)
    lake.write_partition(
        "bodies",
        "2026-10-19",
        "b1",
        [
            {
                "body_hash": "bD",
                "body_text": "Bright and juicy.\n12 oz bag",
                "first_seen_run": date(2026, 10, 19),
            }
        ],
    )
    con = build(lake, tmp_path)
    got = dict(
        (
            (pid, (p100, sizes))
            for pid, p100, sizes in con.execute(
                "select product_id, price_per_100g, bag_sizes_g from dim_coffee order by product_id"
            ).fetchall()
        )
    )
    assert got == {
        "A": (4.41, [454]),
        "B": (3.53, [454, 2268]),
        "C": (3.96, [227, 454]),
        "D": (5.0, [340]),
        "E": (4.41, [1021]),
        "F": (None, None),
        "G": (None, None),
    }
    raw = dict(con.execute("select product_id, [v.grams for v in variants] from stg_snapshots").fetchall())
    assert raw["A"] == [None, None] and raw["C"] == [907, 454] and raw["E"] == [340]
