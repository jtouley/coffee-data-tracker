"""The data-quality rules evaluated in eval/dq (README): per-adapter volume bands (#11), near-duplicate roasters
(#12), price per 100 g outliers (#7) and cross-table reconciliation (#4). Each warns and never fails the build."""

import re
import shutil

import duckdb
import pytest

from .lakehelp import manifest, product, run
from .test_dbt import build, run_dbt
from .test_quality import _passing_builds

pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
WEEK1, WEEK2 = "2026-10-19", "2026-10-26"


def _metrics(con, prefix: str) -> dict[str, float]:
    rows = con.execute(
        "select metric, value from mart_build_profile where metric like ?", [prefix]
    ).fetchall()
    return dict(rows)


def _priced(pid: str, price: float | None) -> dict:
    return product(pid, price, title=f"Kenya {pid} Washed Light Roast")


def _test(lake, tmp_path, name: str, **dbt_vars):
    return run_dbt(lake, tmp_path, "test", "--select", name, **dbt_vars)


def _break(tmp_path, *statements: str) -> None:
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"))
    for s in statements:
        con.execute(s)
    con.close()


def test_the_profile_has_the_per_adapter_volume_metrics(lake, tmp_path):
    """adapter_attempted_ok_share counts only the roasters the run tried; the same-store ratios compare each
    roaster's products, and priced products, with its previous fetch that brought products."""
    run(lake, WEEK1, "w.com", [_priced(str(i), 20.0) for i in range(4)], adapter="woocommerce")
    run(lake, WEEK1, "s.com", [_priced(f"s{i}", 20.0) for i in range(2)])
    run(lake, WEEK1, "t.com", [_priced(f"t{i}", 20.0) for i in range(2)])
    # This week: w.com's fetch "succeeds" with two prices gone (a price wipe), s.com fails, t.com isn't tried.
    run(lake, WEEK2, "w.com", [_priced("0", 20.0), _priced("1", 20.0), _priced("2", None), _priced("3", None)],
        adapter="woocommerce")  # fmt: skip
    run(lake, WEEK2, "s.com", None)
    manifest(lake, WEEK2, ["w.com", "s.com", "t.com"])
    # The run is incomplete and mostly failed, so the floors are off: only the metrics are checked.
    floors = {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}
    con = build(lake, tmp_path, as_of_run=WEEK2, **floors)
    assert _metrics(con, "adapter_attempted_ok_share:%") == {
        "adapter_attempted_ok_share:shopify": 0.0,  # s.com tried and failed; t.com not tried, so not counted
        "adapter_attempted_ok_share:woocommerce": 1.0,
    }
    assert _metrics(con, "adapter_ok_share:%")["adapter_ok_share:shopify"] == 0.0
    assert _metrics(con, "adapter_same_store_%") == {
        "adapter_same_store_products:woocommerce": 1.0,
        "adapter_same_store_priced:woocommerce": 0.5,
    }
    con.close()


def test_a_platforms_price_wipe_warns_against_its_own_history(lake, tmp_path):
    run(lake, WEEK1, "w.com", [_priced(str(i), 20.0) for i in range(4)], adapter="woocommerce")
    run(lake, WEEK2, "w.com", [_priced("0", 20.0), _priced("1", 20.0), _priced("2", None), _priced("3", None)],
        adapter="woocommerce")  # fmt: skip
    manifest(lake, WEEK2, ["w.com"])
    flat = {"adapter_attempted_ok_share:woocommerce": 1.0, "adapter_same_store_products:woocommerce": 1.0,
            "adapter_same_store_priced:woocommerce": 1.0}  # fmt: skip
    floors = {"as_of_run": WEEK2, "volume_tolerance": 1e9, "min_adapter_roasters": 1}
    _passing_builds(lake, [flat] * 5, first="2026-08-31")
    r = build(lake, tmp_path, check=False, **floors)  # 5 builds of history: not banded yet
    assert r.returncode == 0 and re.search(r"PASS adapter_volume_in_band", r.stdout), r.stdout[-3000:]
    _passing_builds(lake, [flat], first="2026-10-05")
    r = build(lake, tmp_path, check=False, **floors)  # 6: priced 0.5 is far below 1.0 - 0.10
    assert r.returncode == 0 and re.search(r"WARN 1 adapter_volume_in_band", r.stdout), r.stdout[-3000:]
    # A platform with fewer than min_adapter_roasters roasters with a catalog is never judged.
    r = build(lake, tmp_path, check=False, **floors | {"min_adapter_roasters": 2})
    assert r.returncode == 0 and re.search(r"PASS adapter_volume_in_band", r.stdout), r.stdout[-3000:]


def test_one_catalog_under_two_roaster_ids_warns_and_generic_titles_do_not(lake, tmp_path):
    lot = ["Kenya Gichathaini AA", "Ethiopia Banko Gotiti Natural", "Colombia Edinson Argote SL28", "Decaf"]
    for rid in ("verve.com", "vervecoffeeroasters.com"):
        run(lake, WEEK2, rid, [product(str(i), title=f"{t} Coffee") for i, t in enumerate(lot)])
    # Four roasters that share only generic titles: each is listed by 4 roasters, so none is rare.
    generic = ["House Blend Coffee", "Espresso Blend Coffee", "French Roast Coffee"]
    for rid in ("a.com", "b.com", "c.com", "d.com"):
        run(lake, WEEK2, rid, [product(f"{rid}{i}", title=t) for i, t in enumerate(generic)])
    r = build(lake, tmp_path, check=False)
    assert r.returncode == 0 and re.search(r"WARN 1 near_duplicate_roasters", r.stdout), r.stdout[-3000:]
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    pairs = con.execute(
        "select roaster_a, roaster_b, shared from main_dbt_test__audit.near_duplicate_roasters"
    )
    assert pairs.fetchall() == [("verve.com", "vervecoffeeroasters.com", 4)]
    con.close()


def test_a_price_per_100g_far_from_its_peers_warns(lake, tmp_path):
    peers = [product(str(i), 14.0 + i * 0.5, title=f"Guatemala Lot {i} Washed Coffee") for i in range(10)]
    pesos = product(
        "mx", 190.0, title="Mexico Chiapas Bourbon Coffee"
    )  # 190 pesos read as dollars: $55.88 per 100 g
    half_pound = product("hp", 6.0, title="Guatemala Huehuetenango Coffee")  # $6 for "1/2 lb" read as 907 g
    half_pound["variants"][0] |= {"title": "1/2 lb", "grams": 907}
    run(lake, WEEK2, "r.com", [*peers, pesos, half_pound])
    r = build(lake, tmp_path, check=False)
    assert r.returncode == 0 and re.search(r"WARN 2 price_per_100g_outliers", r.stdout), r.stdout[-3000:]
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    rows = con.execute("select coffee_id, z > 0 from main_dbt_test__audit.price_per_100g_outliers order by 1")
    assert rows.fetchall() == [("r.com:hp", False), ("r.com:mx", True)]
    con.close()


def test_the_public_tables_reconcile_and_a_reclassified_product_is_no_disagreement(lake, tmp_path):
    """The 2026-11-02 case: products reclassified as not coffee leave dim_coffee and keep their events (24 orphan
    events); that is by design, so the reconciliation passes while fct_price_event's relationships test warns."""
    run(lake, WEEK1, "onyxcoffeelab.com", [product("A"), product("P", title="French Roast Single Cups")])
    run(lake, WEEK2, "onyxcoffeelab.com", [product("A"), product("P", coffee=False, title="Mug P")])
    manifest(lake, WEEK2, ["onyxcoffeelab.com"])
    r = build(lake, tmp_path, check=False, as_of_run=WEEK2)
    assert r.returncode == 0 and re.search(r"PASS public_tables_reconcile", r.stdout), r.stdout[-3000:]
    assert re.search(r"WARN \d+ relationships_fct_price_event_coffee_id", r.stdout), r.stdout[-3000:]
    _break(
        tmp_path,
        "update public_roaster set coffees_listed = coffees_listed + 1",  # roaster_counts
        "update dim_roaster set coffees_listed = coffees_listed + 1",  # listed_total
        "update public_price_event set roaster_id = 'elsewhere.com' where coffee_id like '%:A'",  # event_roaster
        "insert into fct_price_event (coffee_id, roaster_id, event) values ('onyxcoffeelab.com:gone', "
        "'onyxcoffeelab.com', 'new')",  # an orphan that is still coffee (no record at all)
    )
    r = _test(lake, tmp_path, "public_tables_reconcile")
    assert r.returncode == 0 and re.search(r"WARN 4 public_tables_reconcile", r.stdout), r.stdout[-3000:]


def _release(tmp_path, name: str) -> dict[str, str]:
    """Write the built public tables as a release's files, under a releases/run_date=.../build=... path as the lake
    keeps them (the path must not leak into the rows: price events keep their own run_date)."""
    out = tmp_path / "releases" / f"run_date={WEEK2}" / f"build={name}"
    out.mkdir(parents=True)
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    files = {}
    for t in ("public_coffee", "public_price_event", "public_roaster", "public_market_weekly"):
        files[t] = str(out / f"{t}.parquet")
        con.execute(f"copy (select * from {t}) to '{files[t]}' (format parquet)")
    con.close()
    return files


def _changes(tmp_path) -> dict[tuple[str, str, str], int]:
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    rows = con.execute(
        "select table_name, change, reason, count(*) from release_changes group by all"
    ).fetchall()
    con.close()
    return {(t, c, r): n for t, c, r, n in rows}


def test_every_change_against_the_served_release_has_a_reason(lake, tmp_path):
    """release_changes: the new build's public tables against the served release's, row by row, each change with
    the reason the build can show; release_changes_explained warns on the rest. The case of the 2026-11-02 verdict
    backfill (956abd -> 5060ad): a product reclassified as not coffee leaves with its events, and a coffee whose
    earlier version is now coffee too keeps its row but gets its history re-derived (first_seen moves back, its
    'new' event moves to the earlier run and a 'price' event appears)."""
    rid = "onyxcoffeelab.com"
    flip = {"title": "Venus (Espresso, Drip, Cold Brew) Gift Set"}  # the rules read it as not coffee
    run(lake, WEEK1, rid, [product("A"), product("P", title="Single Cups Coffee"), product("V", 24.5, coffee=False,
                                                                                         **flip)])  # fmt: skip
    run(lake, WEEK2, rid, [product("A"), product("P", title="Single Cups Coffee"), product("V", 28.5,
                                                                                         title="Venus Espresso Blend")])  # fmt: skip
    manifest(lake, WEEK2, [rid])
    build(lake, tmp_path, as_of_run=WEEK2).close()
    con = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    assert con.execute(f"select first_seen::varchar from public_coffee where coffee_id = '{rid}:V'").fetchone() == (
        WEEK2,), "the fixture's first version should read as not coffee"  # fmt: skip
    con.close()
    served = _release(tmp_path, "b1")
    # The backfill: V's first version now reads as coffee (its title, as a verdict would), P's latest doesn't.
    run(lake, WEEK1, rid, [product("A"), product("P", title="Single Cups Coffee"), product("V", 24.5,
                                                                                         title="Venus Espresso Blend")], hour=13)  # fmt: skip
    run(lake, WEEK2, rid, [product("A"), product("P", coffee=False, title="Single Cups Mug"), product("V", 28.5,
                                                                                         title="Venus Espresso Blend")], hour=13)  # fmt: skip
    r = build(lake, tmp_path, check=False, as_of_run=WEEK2, previous_release=served)
    assert r.returncode == 0 and re.search(r"PASS release_changes_explained", r.stdout), r.stdout[-3000:]
    changes = _changes(tmp_path)
    assert changes[("public_coffee", "removed", "reclassified")] == 1  # P
    assert changes[("public_coffee", "changed", "history_changed")] == 1  # V
    assert changes[("public_price_event", "removed", "coffee_left")] == 1  # P's 'new'
    assert (
        changes[("public_price_event", "added", "history_changed")] == 2
    )  # V: 'new' a week earlier, 'price'
    assert changes[("public_price_event", "removed", "history_changed")] == 1  # V's old 'new'
    assert not any(reason == "unexplained" for _, _, reason in changes)
    from coffeedata import quality

    notes = quality.release_notes(tmp_path / "coffee.duckdb")
    assert any(n.startswith("public_coffee: ") and "removed 1 (reclassified 1)" in n for n in notes), notes
    # An event that changed on a coffee whose own row didn't is unexplained: it warns, and the build still passes.
    con = duckdb.connect()
    con.execute(f"copy (select * replace (price_usd + 1 as price_usd) from '{served['public_price_event']}') "
                f"to '{tmp_path}/tampered.parquet' (format parquet)")  # fmt: skip
    con.close()
    r = build(lake, tmp_path, check=False, as_of_run=WEEK2,
              previous_release=served | {"public_price_event": f"{tmp_path}/tampered.parquet"})  # fmt: skip
    assert r.returncode == 0 and re.search(r"WARN \d+ release_changes_explained", r.stdout), r.stdout[-3000:]


def test_nothing_served_is_nothing_to_reconcile(lake, tmp_path):
    run(lake, WEEK2, "onyxcoffeelab.com", [product("A")])
    r = build(lake, tmp_path, check=False)
    assert r.returncode == 0 and re.search(r"PASS release_changes_explained", r.stdout), r.stdout[-3000:]
    assert _changes(tmp_path) == {}
