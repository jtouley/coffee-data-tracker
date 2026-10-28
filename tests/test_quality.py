"""Data quality: suspect runs held out of history, overrides, the build-to-build volume and freshness checks, and
the build records and mart history that `coffeedata build-diff` compares."""

import argparse
import shutil
from datetime import UTC, date, datetime

import pytest

from coffeedata import quality
from coffeedata.lake import Lake

from .lakehelp import product, run
from .test_dbt import build

pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
RID = "onyxcoffeelab.com"
FULL = [product(str(i)) for i in range(40)]


def _weeks(lake, *catalogs):
    for d, cat in zip(("2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"), catalogs, strict=False):
        run(lake, d, RID, cat)


def _events(con):
    return sorted(
        con.execute("select run_date, event, count(*) from fct_price_event group by all").fetchall()
    )


def test_a_truncated_week_is_held_out_not_read_as_removals(lake, tmp_path):
    _weeks(lake, FULL, FULL, FULL[:12], FULL)  # week 3 came back with 12 of 40 products
    con = build(lake, tmp_path)
    q = con.execute("select run_date, suspect, counted from stg_run_quality order by 1").fetchall()
    assert [(str(d), s, c) for d, s, c in q] == [
        ("2026-10-05", False, True), ("2026-10-12", False, True),
        ("2026-10-19", True, False), ("2026-10-26", False, True),
    ]  # fmt: skip
    assert _events(con) == [(date(2026, 10, 5), "new", 40)]  # no false "removed", no false "relisted"


def test_overrides_accept_reject_and_pin_a_batch(lake, tmp_path):
    _weeks(lake, FULL, FULL, FULL[:12])
    quality.add_override(lake, RID, "2026-10-19", "accept", "roaster really cut its catalog", None)
    assert (date(2026, 10, 19), "removed", 28) in _events(build(lake, tmp_path))

    quality.add_override(lake, RID, "2026-10-12", "reject", "bad scrape", None)  # the latest override wins
    weeks = build(lake, tmp_path).execute("select run_date from stg_runs order by 1").fetchall()
    assert [str(w[0]) for w in weeks] == ["2026-10-05", "2026-10-19"]

    first = run(lake, "2026-10-26", RID, FULL[:30])
    run(lake, "2026-10-26", RID, FULL[:31], hour=18)  # latest batch of the week would win...
    quality.add_override(lake, RID, "2026-10-26", "use_batch", "the morning fetch was complete", first)
    con = build(lake, tmp_path)  # ...but the pinned one counts
    assert con.execute("select batch_id from stg_runs where run_date = '2026-10-26'").fetchone()[0] == first

    with pytest.raises(ValueError, match="use_batch needs"):
        quality.add_override(lake, RID, "2026-10-26", "use_batch", "x", None)
    with pytest.raises(ValueError, match="say why"):
        quality.add_override(lake, RID, "2026-10-26", "reject", " ", None)


def _baseline(lake, listed: float):
    now = datetime(2026, 10, 20, tzinfo=UTC)
    lake.write("builds", "builds/run_date=2026-10-19/build=b1.parquet", [
        {"build_id": "b1", "built_at": now, "kind": "node", "unique_id": "test.x", "status": "pass"},
        {"build_id": "b1", "built_at": now, "kind": "metric", "unique_id": "listed_coffees", "value": listed},
    ])  # fmt: skip


def test_volume_check_fails_a_collapsed_build_unless_accepted(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)
    _baseline(lake, 100.0)  # last passing build listed 100; this one lists 40
    r = build(lake, tmp_path, check=False)
    assert r.returncode != 0 and "catalog_volume_vs_last_build" in r.stdout
    build(lake, tmp_path, volume_tolerance=1e9)  # --accept-volume-change


def test_volume_check_ignores_failed_builds_as_baselines(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)
    _baseline(lake, 40.0)
    later = datetime(2026, 10, 27, tzinfo=UTC)
    lake.write("builds", "builds/run_date=2026-10-26/build=b2.parquet", [
        {"build_id": "b2", "built_at": later, "kind": "node", "unique_id": "test.y", "status": "fail"},
        {"build_id": "b2", "built_at": later, "kind": "metric", "unique_id": "listed_coffees", "value": 5.0},
    ])  # fmt: skip
    build(lake, tmp_path)  # compared with b1 (40), not the failed b2 (5)


def test_freshness_fails_on_a_stale_catalog(lake, tmp_path):
    run(lake, "2026-10-05", RID, FULL)
    assert build(lake, tmp_path, as_of_run="2026-10-10", min_attempted_share=0, min_ok_share=0)
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26")
    assert r.returncode != 0 and "latest_run_is_fresh" in r.stdout


SEED = ("1802roasters.com", "anaheimcoffee.com", "counterculturecoffee.com", "intelligentsia.com",
        "onyxcoffeelab.com", "vividcoffee.com")  # tests/fixtures/roasters.csv  # fmt: skip


def test_completeness_catches_a_skipped_roaster_and_a_mostly_failed_run(lake, tmp_path):
    for rid in SEED[1:]:  # 1802roasters.com is never tried: ingestion skipped it
        run(lake, "2026-10-26", rid, FULL[:5])
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26")
    assert r.returncode != 0 and "run_is_complete" in r.stdout
    con = build(lake, tmp_path, min_attempted_share=0.5)  # (as_of_run unset: the latest run, no gate)
    got = con.execute("select roaster_id, attempted, ok from run_completeness order by 1").fetchall()
    assert got[0] == ("1802roasters.com", False, False) and all(a and o for _, a, o in got[1:])

    run(lake, "2026-10-26", SEED[0], None)  # tried, and failed: complete, but 5 of 6 ok
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26")
    assert (
        r.returncode != 0
        and "run_ok_rate" in r.stdout
        and "run_is_complete" not in r.stdout.split("Done.")[-1]
    )
    con = build(lake, tmp_path, as_of_run="2026-10-26", min_ok_share=0.8)
    metrics = dict(con.execute("select metric, value from mart_build_profile").fetchall())
    assert metrics["run_roasters_expected"] == 6 and metrics["run_attempted_share"] == 1.0
    assert round(metrics["run_ok_share"], 3) == 0.833


def test_releases_are_recorded_served_gated_and_rolled_back(lake, monkeypatch, tmp_path):
    from coffeedata import cli

    real_dbt_build = cli.dbt_build
    # The real seed list has ~2,700 roasters this lake never fetches; completeness has its own test above.
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real_dbt_build(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0}))  # fmt: skip
    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))
    monkeypatch.setattr(cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event"])
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")

    def args(d):
        return argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date=d, accept_volume_change=False)

    current = lake.root / "marts" / "dim_coffee.parquet"
    run(lake, "2026-10-19", RID, FULL[:30])
    assert cli.cmd_build(args("2026-10-19")) is True
    first_release = current.read_bytes()
    run(lake, "2026-10-26", RID, FULL[5:32])  # 5 gone, 2 new: within tolerance
    assert cli.cmd_build(args("2026-10-26")) is True
    second_release = current.read_bytes()
    b1, b2 = (b for b, _, _ in quality.builds(Lake()))

    # No fetch for weeks: every model and check still runs and the release is kept, but it fails
    # latest_run_is_fresh, so customers keep getting the second release (the automatic rollback).
    assert cli.cmd_build(args("2026-11-30")) is False
    assert current.read_bytes() == second_release and quality.served(Lake())["build_id"] == b2
    b3 = quality.builds(Lake())[-1][0]
    assert [ok for _, _, ok in quality.builds(Lake())] == [True, True, False]
    for b in (b1, b2, b3):  # every build's release, passing or not, with every table customers read
        assert set(quality.release_files(Lake(), b)) == set(quality.RELEASE_TABLES)
    with pytest.raises(ValueError, match="failed its checks"):
        quality.serve(Lake(), b3, "no")

    failed = quality.build_diff(Lake())  # default: the latest build against the last passing one
    assert failed["new_passed"] is False and failed["failing"] == ["test.coffeedata.latest_run_is_fresh"]
    assert failed["coffees"]["delisted"] == 0  # the held-back build would have changed no coffee

    d = quality.build_diff(Lake(), b1, b2)
    assert d["new_passed"] is True and d["failing"] == []
    assert d["code"] == {"added": [], "removed": [], "changed": []}
    assert d["metrics"]["listed_coffees"] == {"old": 30.0, "new": 27.0}
    assert d["coffees"] == {
        "coffees_new": 2, "coffees_gone": 0, "delisted": 5, "relisted": 0,
        "went_out_of_stock": 5, "came_back_in_stock": 0,
    }  # fmt: skip
    assert d["roasters_most_changed"] == [(RID, 30, 27)]

    # A problem found later: roll back to the release before the served one, with the reason on record.
    assert quality.rollback(Lake(), reason="bad prices from one roaster") == b1
    assert current.read_bytes() == first_release
    assert quality.served(Lake())["build_id"] == b1
    assert quality.served(Lake())["reason"] == "bad prices from one roaster"
