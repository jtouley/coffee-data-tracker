"""Data quality: suspect runs held out of history, overrides, the build-to-build volume and freshness checks, and
the build records and mart history that `coffeedata build-diff` compares."""

import argparse
import json
import re
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from coffeedata import quality
from coffeedata.lake import Lake

from .lakehelp import manifest, old_file, product, run
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


def test_a_store_that_stays_broken_stays_flagged(lake, tmp_path):
    # The baseline learns only from counted weeks, so five broken weeks in a row never become the new normal.
    dates = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]
    for d, cat in zip(dates, [FULL, FULL] + [FULL[:12]] * 5, strict=True):
        run(lake, d, RID, cat)
    con = build(lake, tmp_path)
    q = con.execute("select suspect, counted from stg_run_quality order by run_date").fetchall()
    assert q == [(False, True)] * 2 + [(True, False)] * 5


def test_use_batch_with_a_batch_that_does_not_exist_is_no_override(lake, tmp_path):
    _weeks(lake, FULL, FULL, FULL[:12])
    lake.write("run_overrides", "run_overrides/override_id=manual.parquet", [{
        "added_at": datetime(2026, 10, 20, tzinfo=UTC), "roaster_id": RID, "run_date": date(2026, 10, 19),
        "action": "use_batch", "batch_id": "no-such-batch", "reason": "typo"}])  # fmt: skip
    con = build(lake, tmp_path)
    assert con.execute("select suspect, counted from stg_run_quality where run_date = '2026-10-19'").fetchone() == (
        True, False)  # fmt: skip


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
    with pytest.raises(ValueError, match="no fetch of"):  # pin only a batch that exists for that roaster-week
        quality.add_override(lake, RID, "2026-10-26", "use_batch", "x", "no-such-batch")
    failed = run(lake, "2026-11-02", RID, None)
    with pytest.raises(ValueError, match="only a successful fetch"):
        quality.add_override(lake, RID, "2026-11-02", "use_batch", "x", failed)


def _baseline(lake, listed: float, build: str = "b1", day: int = 20):
    """A passing build that listed `listed` coffees, built on 2026-10-<day>."""
    now = datetime(2026, 10, day, tzinfo=UTC)
    lake.write("builds", f"builds/run_date=2026-10-19/build={build}.parquet", [
        {"build_id": build, "built_at": now, "kind": "build", "unique_id": "build", "status": "pass"},
        {"build_id": build, "built_at": now, "kind": "node", "unique_id": "test.x", "status": "pass"},
        {"build_id": build, "built_at": now, "kind": "metric", "unique_id": "listed_coffees", "value": listed},
    ])  # fmt: skip


def test_volume_check_fails_a_collapsed_build_unless_accepted(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)
    _baseline(lake, 100.0)  # last passing build listed 100; this one lists 40
    r = build(lake, tmp_path, check=False)
    assert r.returncode != 0 and "catalog_volume_vs_last_build" in r.stdout
    build(lake, tmp_path, volume_tolerance=1e9)  # --accept-volume-change


def test_carried_forward_catalogs_do_not_hide_an_outage(lake, tmp_path):
    # Last week 6 roasters listed 60 coffees; this week 4 of them refuse. Their catalogs stay listed (carried
    # forward), so listed_coffees holds at 60, but the coffees this run fetched fall to 20: the volume check fails
    # on that metric, and the ok rate and freshness still count the failed fetches.
    for rid in SEED:
        run(lake, "2026-10-19", rid, FULL[:10])
        run(lake, "2026-10-26", rid, FULL[:10] if rid in SEED[:2] else None)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    lake.write("builds", "builds/run_date=2026-10-19/build=b1.parquet", [
        {"build_id": "b1", "built_at": now, "kind": "build", "unique_id": "build", "status": "pass"},
        {"build_id": "b1", "built_at": now, "kind": "metric", "unique_id": "listed_coffees", "value": 60.0},
        {"build_id": "b1", "built_at": now, "kind": "metric", "unique_id": "fresh_listed_coffees", "value": 60.0},
    ])  # fmt: skip
    gate = {"as_of_run": "2026-10-26", "min_ok_share": 0.3}  # each check alone (a failure skips what follows)
    r = build(lake, tmp_path, check=False, **gate)
    assert r.returncode != 0 and re.search(r"FAIL 1 catalog_volume_vs_last_build", r.stdout)  # fresh only
    r = build(lake, tmp_path, check=False, **gate | {"min_ok_share": 0.9, "volume_tolerance": 1e9})
    assert r.returncode != 0 and re.search(r"FAIL 1 run_ok_rate", r.stdout)
    r = build(lake, tmp_path, check=False, **gate | {"volume_tolerance": 1e9, "today": "2026-11-20"})
    assert r.returncode != 0 and re.search(r"FAIL 1 latest_run_is_fresh", r.stdout)
    con = build(lake, tmp_path, **gate | {"volume_tolerance": 1e9})
    metrics = dict(con.execute("select metric, value from mart_build_profile").fetchall())
    assert (metrics["listed_coffees"], metrics["fresh_listed_coffees"]) == (60, 20)
    assert (metrics["carried_forward_coffees"], metrics["carried_forward_roasters"]) == (40, 4)
    assert round(metrics["run_catalog_ok_share"], 3) == 0.333


def test_volume_check_ignores_failed_builds_as_baselines(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)
    _baseline(lake, 40.0)
    later = datetime(2026, 10, 27, tzinfo=UTC)
    lake.write("builds", "builds/run_date=2026-10-26/build=b2.parquet", [
        {"build_id": "b2", "built_at": later, "kind": "build", "unique_id": "build", "status": "fail"},
        {"build_id": "b2", "built_at": later, "kind": "node", "unique_id": "test.y", "status": "fail"},
        {"build_id": "b2", "built_at": later, "kind": "metric", "unique_id": "listed_coffees", "value": 5.0},
    ])  # fmt: skip
    build(lake, tmp_path)  # compared with b1 (40), not the failed b2 (5)


def test_an_accepted_volume_change_is_the_next_baseline(lake, tmp_path):
    # A real drop from 100 to 40, let through with --accept-volume-change and served: the next week at 40 compares
    # with 40 and passes, so one accept is enough.
    run(lake, "2026-10-26", RID, FULL)  # lists 40
    _baseline(lake, 100.0, "b0", day=10)
    _baseline(lake, 40.0, "b1", day=11)  # the accepted build
    build(lake, tmp_path, baseline_build="b1").close()
    build(lake, tmp_path).close()  # nothing served yet: the newest passing build, b1


def test_the_volume_check_compares_with_the_served_build(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)
    _baseline(lake, 40.0, "b1", day=20)
    for i in range(3):  # passing builds that were rolled back from: customers still get b1
        _baseline(lake, 100.0, f"later{i}", day=21 + i)
    build(lake, tmp_path, baseline_build="b1").close()  # 40 against b1's 40; the later builds don't count
    r = build(lake, tmp_path, check=False)  # nothing served: the newest passing build, at 100
    assert r.returncode != 0 and "catalog_volume_vs_last_build" in r.stdout


def test_dbt_and_quality_builds_agree_on_which_builds_passed(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)
    records = {  # build -> its (kind, status) rows
        "passed": [("build", "pass"), ("node", "pass"), ("metric", None)],
        "warned": [("build", "pass"), ("node", "warn")],
        "failed": [("build", "fail"), ("node", "pass")],
        "failed_test": [("build", "pass"), ("node", "fail")],
        "errored_model": [("build", "pass"), ("node", "error")],
        "no_verdict": [("node", "pass")],
    }
    for day, (b, rows) in enumerate(records.items(), start=10):
        lake.write("builds", f"builds/run_date=2026-10-19/build={b}.parquet", [
            {"build_id": b, "run_date": date(2026, 10, 19), "built_at": datetime(2026, 10, day, tzinfo=UTC),
             "kind": kind, "unique_id": f"{kind}.{i}", "status": status} for i, (kind, status) in enumerate(rows)
        ])  # fmt: skip
    con = build(lake, tmp_path)
    in_dbt = con.execute(
        "select build_id, run_date::varchar, passed from stg_builds order by built_at, build_id"
    )
    assert in_dbt.fetchall() == quality.builds(lake)
    assert [b for b, _, ok in quality.builds(lake) if ok] == ["passed", "warned"]
    con.close()


def test_freshness_fails_on_a_stale_catalog(lake, tmp_path):
    run(lake, "2026-10-05", RID, FULL)
    assert build(lake, tmp_path, today="2026-10-10", min_attempted_share=0, min_ok_share=0)
    r = build(lake, tmp_path, check=False, today="2026-10-26")
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
    con.close()  # DuckDB reuses an open database for the same path, so a later build() would read this one

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
    # No run manifest (a run from before them): the expected set is reconstructed.
    assert con.execute("select distinct expected_from from run_completeness").fetchall() == [
        ("reconstructed",)
    ]


def test_completeness_expects_what_the_run_manifest_says(lake, tmp_path):
    # The run set out to fetch three seed roasters and one added since; the other seed roasters weren't in it.
    manifest(lake, "2026-10-26", ["anaheimcoffee.com", "onyxcoffeelab.com"])
    manifest(lake, "2026-10-26", ["vividcoffee.com", "new-roaster.com"], mode="retry_failed")  # unioned
    manifest(lake, "2026-10-19", list(SEED))  # another run's manifest doesn't count
    for rid in ("anaheimcoffee.com", "onyxcoffeelab.com", "vividcoffee.com"):
        run(lake, "2026-10-26", rid, FULL[:5])
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26")
    assert r.returncode != 0 and "run_is_complete" in r.stdout  # new-roaster.com was intended, never tried
    con = build(lake, tmp_path, min_attempted_share=0.5, min_ok_share=0, as_of_run="2026-10-26")
    got = con.execute(
        "select roaster_id, attempted, expected_from from run_completeness order by 1"
    ).fetchall()
    assert got == [
        ("anaheimcoffee.com", True, "manifest"), ("new-roaster.com", False, "manifest"),
        ("onyxcoffeelab.com", True, "manifest"), ("vividcoffee.com", True, "manifest"),
    ]  # fmt: skip
    con.close()
    run(lake, "2026-10-26", "new-roaster.com", None)  # tried (and failed): complete by the manifest's account
    build(lake, tmp_path, as_of_run="2026-10-26", min_ok_share=0.7).close()


def test_a_partial_ingests_manifest_never_shrinks_the_expected_set(lake, tmp_path):
    # `weekly --sample 2`: the sample's manifest lists only what it fetched, but the run is not the full week.
    manifest(lake, "2026-10-26", ["anaheimcoffee.com", "onyxcoffeelab.com"], mode="sample")
    for rid in ("anaheimcoffee.com", "onyxcoffeelab.com"):
        run(lake, "2026-10-26", rid, FULL[:5])
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26")
    assert r.returncode != 0 and re.search(r"FAIL \d+ run_is_complete", r.stdout)
    con = build(lake, tmp_path, min_attempted_share=0, min_ok_share=0, as_of_run="2026-10-26")
    got = con.execute("select count(*), sum(attempted::int), any_value(expected_from) from run_completeness")
    assert got.fetchone() == (len(SEED), 2, "reconstructed")
    con.close()
    # A mixed week: a full run from before manifests crashed after two roasters, then `ingest --only` (new code)
    # fetched a third. Its one-row manifest doesn't switch off the reconstruction either.
    manifest(lake, "2026-10-26", ["vividcoffee.com"], mode="only")
    run(lake, "2026-10-26", "vividcoffee.com", FULL[:5])
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26")
    assert r.returncode != 0 and re.search(r"FAIL \d+ run_is_complete", r.stdout)
    # A full ingest's manifest is the expected set; the earlier partial ones still only add to it.
    manifest(lake, "2026-10-26", ["anaheimcoffee.com", "onyxcoffeelab.com"], mode="full")
    con = build(lake, tmp_path, as_of_run="2026-10-26")
    got = con.execute("select roaster_id, expected_from from run_completeness order by 1").fetchall()
    assert got == [(r, "manifest") for r in ("anaheimcoffee.com", "onyxcoffeelab.com", "vividcoffee.com")]
    con.close()


def test_no_catalog_stores_do_not_pad_the_ok_share(lake, tmp_path):
    # The gap the real runs showed: ~600 no_catalog stores "succeed" with 0 products and padded the share.
    for rid in SEED[:3] + ("empty-store.com",):
        run(lake, "2026-10-19", rid, [])
    for rid in SEED[3:] + ("emptied.com",):
        run(lake, "2026-10-19", rid, FULL[:5])
    run(lake, "2026-10-19", "never-ok.com", None)
    manifest(lake, "2026-10-26", [*SEED, "empty-store.com", "emptied.com", "never-ok.com"])
    for rid in SEED[:3]:
        run(lake, "2026-10-26", rid, [])  # still no catalog: not counted
    run(lake, "2026-10-26", "empty-store.com", None)  # failed after only empty successes: not counted
    run(lake, "2026-10-26", SEED[3], FULL[:5])
    run(lake, "2026-10-26", SEED[4], FULL[:5])
    run(lake, "2026-10-26", SEED[5], None)
    run(lake, "2026-10-26", "emptied.com", [])  # had products: counted, and 0 products isn't a success
    run(lake, "2026-10-26", "never-ok.com", None)  # unknown: counted
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26", min_ok_share=0.6)
    assert r.returncode != 0 and re.search(r"FAIL 1 run_ok_rate", r.stdout)  # 2 of 5, though 6 of 9 "ok"
    con = build(lake, tmp_path, as_of_run="2026-10-26", min_ok_share=0.4)
    got = con.execute(
        "select roaster_id, has_catalog, catalog_ok from run_completeness order by 1"
    ).fetchall()
    assert got == [
        ("1802roasters.com", False, False), ("anaheimcoffee.com", False, False),
        ("counterculturecoffee.com", False, False), ("emptied.com", True, False), ("empty-store.com", False, False),
        ("intelligentsia.com", True, True), ("never-ok.com", True, False), ("onyxcoffeelab.com", True, True),
        ("vividcoffee.com", True, False),
    ]  # fmt: skip
    metrics = dict(con.execute("select metric, value from mart_build_profile").fetchall())
    assert round(metrics["run_ok_share"], 3) == 0.667 and metrics["run_catalog_roasters"] == 5
    assert metrics["run_catalog_ok_share"] == 0.4
    assert metrics["adapter_ok_share:shopify"] == 0.4 and metrics["adapter_roasters:shopify"] == 5
    con.close()


def test_one_adapters_total_outage_fails_the_run(lake, tmp_path):
    shops, squares = [f"shop{i}.com" for i in range(4)], ["sq0.com", "sq1.com"]
    manifest(lake, "2026-10-26", [*shops, *squares, "woo.com"])
    for rid in shops:
        run(lake, "2026-10-26", rid, FULL[:5])
    for rid in squares:
        run(lake, "2026-10-26", rid, None, adapter="squarespace")
    run(lake, "2026-10-26", "woo.com", None, adapter="woocommerce")  # one roaster: too few to judge alone
    gate = {"as_of_run": "2026-10-26", "min_ok_share": 0.5, "min_adapter_roasters": 2}
    r = build(lake, tmp_path, check=False, **gate)  # 4 of 7 overall clears 0.5; squarespace is 0 of 2
    assert r.returncode != 0 and re.search(r"FAIL 1 adapter_ok_rate", r.stdout)
    run(lake, "2026-10-26", "sq0.com", FULL[:5], hour=14, adapter="squarespace")  # the retry got one back
    build(lake, tmp_path, **gate).close()  # squarespace 1 of 2 is at the floor; woocommerce isn't judged


def _passing_builds(lake, metrics: list[dict], first: str = "2026-09-07"):
    """One passing build per weekly run from `first` on, recording each dict of metrics."""
    for i, m in enumerate(metrics):
        d = date.fromisoformat(first) + timedelta(weeks=i)
        b, now = f"h{d:%m%d}", datetime(d.year, d.month, d.day, 20, tzinfo=UTC)
        base = {"build_id": b, "run_date": d, "built_at": now}
        lake.write("builds", f"builds/run_date={d}/build={b}.parquet", [
            base | {"kind": "build", "unique_id": "build", "status": "pass"},
            *(base | {"kind": "metric", "unique_id": k, "value": float(v)} for k, v in m.items()),
        ])  # fmt: skip


def test_bands_pass_normal_noise(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)  # 40 listed, every roaster with a catalog fetched
    manifest(lake, "2026-10-26", [RID])
    listed, ok = [41, 39, 40, 42, 38, 40], [0.97, 0.95, 0.96, 0.97, 0.96, 0.95]
    _passing_builds(
        lake, [{"listed_coffees": n, "run_catalog_ok_share": s} for n, s in zip(listed, ok, strict=True)]
    )
    # Within the band; an ok share above the band is no problem.
    build(lake, tmp_path, as_of_run="2026-10-26").close()


def test_bands_flag_an_outlier_once_there_is_enough_history(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)  # 40 listed
    run(lake, "2026-10-26", "down.com", None)  # and half the run failed: ok share 0.5
    manifest(lake, "2026-10-26", [RID, "down.com"])
    history = [{"listed_coffees": n, "run_catalog_ok_share": 0.97} for n in (100, 104, 98, 101, 97)]
    _passing_builds(lake, history)
    # The fixed checks pass.
    floors = {"as_of_run": "2026-10-26", "volume_tolerance": 1e9, "min_ok_share": 0.4}
    build(lake, tmp_path, **floors).close()  # 5 builds of history: not banded yet, only the fixed thresholds
    _passing_builds(lake, [{"listed_coffees": 103, "run_catalog_ok_share": 0.97}], first="2026-10-12")
    r = build(lake, tmp_path, check=False, **floors)  # 6: banded, and 40 and 0.5 are far out
    # Flagged, but only a warning for now: the fixed floors decide, so the build still passes.
    assert r.returncode == 0 and re.search(r"WARN 2 build_metrics_in_band", r.stdout)
    # The band is k x 1.4826 x MAD wide: a huge k lets 40 through, but the ok share's history is flat (MAD 0), so
    # its band is the minimum half-width, and 0.5 is still out of it.
    r = build(lake, tmp_path, check=False, **floors, band_k=1000)
    assert r.returncode == 0 and re.search(r"WARN 1 build_metrics_in_band", r.stdout)


def test_a_new_accept_override_restarts_the_bands(lake, tmp_path):
    run(lake, "2026-10-26", RID, FULL)  # 40 listed, after a level of 100
    manifest(lake, "2026-10-26", [RID])
    _passing_builds(
        lake, [{"listed_coffees": n, "accepted_overrides": 0} for n in (100, 104, 98, 101, 97, 103)]
    )
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26", volume_tolerance=1e9)
    assert re.search(r"WARN \d+ build_metrics_in_band", r.stdout)
    quality.add_override(lake, RID, "2026-10-26", "accept", "the roaster cut its range", None)
    r = build(lake, tmp_path, check=False, as_of_run="2026-10-26", volume_tolerance=1e9)
    assert r.returncode == 0 and not re.search(r"WARN \d+ build_metrics_in_band", r.stdout)


def test_an_accepted_drop_restarts_the_bands_so_the_new_level_passes(lake, monkeypatch, tmp_path):
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    monkeypatch.setattr(cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event"])
    # Six weeks around 100 listed coffees: the bands are active.
    _passing_builds(
        lake, [{"listed_coffees": n, "in_stock_coffees": n} for n in (100, 104, 98, 101, 97, 103)]
    )

    def week(d, products, accept=False):
        if products is not None:
            run(lake, d, RID, products)
            manifest(lake, d, [RID])
        monkeypatch.setattr(cli, "_today", lambda: d)
        args = argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date=d, accept_volume_change=accept)
        return cli.cmd_build(args)

    assert week("2026-10-19", FULL) is False  # a real drop to 40: out of band, and more than volume_tolerance
    band = (
        Lake()
        .connect()
        .execute(  # out of band too, reported as a warning (the volume check is what fails)
            f"select status from {Lake().scan('builds')} where build_id = ? and unique_id = ?",
            [quality.builds(Lake())[-1][0], "test.coffeedata.build_metrics_in_band"],
        )
        .fetchone()
    )
    assert band == ("warn",)
    assert week("2026-10-19", None, accept=True) is True  # reviewed: let through once
    accepted = quality.builds(Lake())[-1][0]
    metrics = dict(Lake().connect().execute(
        f"select unique_id, value from {Lake().scan('builds')} where build_id = ? and kind = 'metric'", [accepted]
    ).fetchall())  # fmt: skip
    assert metrics["accepted_volume_change"] == 1.0
    # The next week at the new level passes: the bands' history starts at the accepted build (one build: not
    # banded yet), where without the reset its median would still be 100.
    assert week("2026-10-26", FULL[:39]) is True


def test_a_run_killed_after_its_manifest_is_still_the_run_judged(lake, tmp_path):
    for rid in SEED:  # last week: complete
        run(lake, "2026-10-19", rid, FULL[:5])
    manifest(lake, "2026-10-26", list(SEED))  # this week's full ingest died before its first fetch
    assert quality.latest_run(lake) == "2026-10-26"
    r = build(lake, tmp_path, check=False, as_of_run=quality.latest_run(lake))
    assert r.returncode != 0 and re.search(r"FAIL \d+ run_is_complete", r.stdout)
    con = build(lake, tmp_path, min_attempted_share=0, min_ok_share=0)  # no as_of_run: the default run CTE
    got = con.execute("select distinct run_date::varchar, attempted from run_completeness").fetchall()
    assert got == [("2026-10-26", False)]
    con.close()


def test_served_is_none_only_when_nothing_is_served(lake, monkeypatch):
    assert quality.served(lake) is None  # a new lake: no marts/release.parquet

    def fail(self, *a, **k):
        raise OSError("HTTP Error: HTTP GET error on 'https://huggingface.co/x' (HTTP 429 Too Many Requests)")

    monkeypatch.setattr(Lake, "connect", fail)
    with pytest.raises(OSError, match="429"):  # never recorded as "nothing served before"
        quality.served(lake)


def test_older_raw_files_without_the_new_columns_build_the_same_tables(lake, tmp_path):
    # Week 1 as older code wrote it (no fetch diagnostics, no payload hashes); week 2 with them.
    old_fetch = [
        "pages",
        "requests",
        "http_statuses",
        "retries",
        "rate_limited",
        "duration_s",
        "code_version",
    ]
    week1 = date(2026, 10, 19)
    old_file(lake.root, "fetches", "fetches/run_date=2026-10-19/batch=old.parquet", old_fetch, {
        "run_date": week1, "batch_id": "old", "fetched_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
        "roaster_id": RID, "adapter": "shopify", "status": "ok", "products": 2, "coffees": 2})  # fmt: skip
    old_file(lake.root, "snapshots", "snapshots/run_date=2026-10-19/batch=old.parquet", ["payload_hash"], *(
        p | {"run_date": week1, "batch_id": "old", "fetched_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
             "roaster_id": RID} for p in FULL[:2]))  # fmt: skip
    run(lake, "2026-10-26", RID, [p | {"payload_hash": f"h{p['product_id']}"} for p in FULL[:2]])
    con = build(lake, tmp_path, as_of_run="2026-10-26", min_attempted_share=0, min_ok_share=0)
    assert con.execute("select count(*) from stg_runs").fetchone()[0] == 2
    assert _events(con) == [(date(2026, 10, 19), "new", 2)]  # the same products both weeks: no change
    for t in ("stg_snapshots", "dim_coffee", "fct_price_event"):  # lake metadata, never in a mart
        assert "payload_hash" not in [c[0] for c in con.execute(f"describe {t}").fetchall()], t


def test_releases_are_recorded_served_gated_and_rolled_back(lake, monkeypatch, tmp_path):
    from coffeedata import cli

    real_dbt_build = cli.dbt_build
    # The real seed list has ~2,700 roasters this lake never fetches; completeness has its own test above.
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real_dbt_build(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}))  # fmt: skip
    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))
    monkeypatch.setattr(
        cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event", "fct_shot"]
    )
    passed_vars = []
    real_dbt = cli.dbt_build
    monkeypatch.setattr(
        cli, "dbt_build", lambda lk, db, extra=None: passed_vars.append(extra) or real_dbt(lk, db, extra)
    )
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    for k, v in {"GITHUB_RUN_ID": "77", "GITHUB_SERVER_URL": "https://github.com",
                 "GITHUB_REPOSITORY": "me/coffee", "GITHUB_ACTOR": "weekly-bot"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)

    def args(d):
        return argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date=d, accept_volume_change=False)

    current = lake.root / "marts" / "dim_coffee.parquet"
    run(lake, "2026-10-19", RID, FULL[:30])
    monkeypatch.setattr(cli, "_today", lambda: "2026-10-19")  # each build on its run's day: fresh
    reads = []
    real_served = quality.served
    monkeypatch.setattr(quality, "served", lambda lk: reads.append(1) or real_served(lk))
    assert cli.cmd_build(args("2026-10-19")) is True
    assert len(reads) == 1  # nothing served yet is an answer too: serve doesn't read it again
    monkeypatch.setattr(quality, "served", real_served)
    first_release = current.read_bytes()
    # What's served has exactly the built columns: a release's path (run_date=.../build=...) must not add any.
    import duckdb
    import yaml

    built = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    cols = lambda c, t: [r[0] for r in c.execute(f"describe {t}").fetchall()]  # noqa: E731
    assert cols(duckdb.connect(), f"select * from '{current}'") == cols(built, "dim_coffee")
    served_db = duckdb.connect(str(cli.SERVED_DB), read_only=True)
    contract = yaml.safe_load(Path("contracts/public-catalog.odcs.yaml").read_text())
    for obj in contract["schema"]:
        assert cols(served_db, obj["name"]) == [p["name"] for p in obj["properties"]], obj["name"]
    served_db.close()
    built.close()
    run(lake, "2026-10-26", RID, FULL[5:32])  # 5 gone, 2 new: within tolerance
    monkeypatch.setattr(cli, "_today", lambda: "2026-10-26")
    reads = []
    real_served = quality.served
    monkeypatch.setattr(quality, "served", lambda lk: reads.append(1) or real_served(lk))
    assert cli.cmd_build(args("2026-10-26")) is True
    assert len(reads) == 1  # serve logs the release the build already read, without reading it again
    monkeypatch.setattr(quality, "served", real_served)
    second_release = current.read_bytes()
    b1, b2 = (b for b, _, _ in quality.builds(Lake()))
    # Every served row says which build and which run it came from.
    lineage = (
        duckdb.connect()
        .execute(f"select distinct release_build_id, release_run_date::varchar from '{current}'")
        .fetchall()
    )
    assert lineage == [(b2, "2026-10-26")]

    # No fetch for weeks (a stopped weekly job): a rebuild on 2026-11-30 still runs every model and check and
    # keeps its release, but fails latest_run_is_fresh, so customers keep the second release (automatic rollback).
    monkeypatch.setattr(cli, "_today", lambda: "2026-11-30")
    shots_mart = lake.root / "marts" / "fct_shot.parquet"
    shots_mart.unlink()
    assert cli.cmd_build(args(None)) is False
    assert current.read_bytes() == second_release and quality.served(Lake())["build_id"] == b2
    assert (
        passed_vars[-1]["as_of_run"] == "2026-10-26"
    )  # no --run-date: the latest run in the lake, not "today"
    assert shots_mart.exists()  # personal marts don't wait on catalog checks
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
    reads = []
    real_served = quality.served
    monkeypatch.setattr(quality, "served", lambda lk: reads.append(1) or real_served(lk))
    assert quality.rollback(Lake(), reason="bad prices from one roaster") == b1
    assert len(reads) == 1  # serve logs the release rollback replaced, without reading it again
    monkeypatch.setattr(quality, "served", real_served)
    assert current.read_bytes() == first_release
    assert quality.served(Lake())["build_id"] == b1
    assert quality.served(Lake())["reason"] == "bad prices from one roaster"
    # The site workflow renders the served release and writes nothing to the lake.
    before = sorted((p.as_posix(), p.stat().st_mtime_ns) for p in lake.root.rglob("*") if p.is_file())
    site_db, site_out = tmp_path / "site.duckdb", tmp_path / "site"
    cli.main(["site", "--served", "--db", str(site_db), "--out", str(site_out)])
    assert (site_out / "index.html").exists()
    assert {r[0] for r in duckdb.connect(str(site_db), read_only=True).execute(
        "select distinct release_build_id from public_coffee").fetchall()} == {b1}  # fmt: skip
    assert sorted((p.as_posix(), p.stat().st_mtime_ns) for p in lake.root.rglob("*") if p.is_file()) == before
    # marts/release.parquet holds only the current release; the serving log keeps every serve and rollback.
    log = (
        Lake().connect()
        .execute(f"select action, build_id, run_date::varchar, reason, actor, previous_build_id, ci_run_url "
                 f"from {Lake().scan('serving_log')} where build_id is not null order by served_at, action desc")
        .fetchall()
    )  # fmt: skip
    url = "https://github.com/me/coffee/actions/runs/77"
    assert log == [
        ("serve", b1, "2026-10-19", "build for 2026-10-19", "weekly-bot", None, url),
        ("serve", b2, "2026-10-26", "build for 2026-10-26", "weekly-bot", b1, url),
        ("rollback", b1, "2026-10-19", "bad prices from one roaster", "weekly-bot", b2, url),
    ]
    # Every build row carries the build's environment.
    env = (
        Lake().connect()
        .execute(f"select build_id, ci_run_id, ci_run_url, dbt_version, package_versions, duration_s "
                 f"from {Lake().scan('builds')} where kind = 'build' order by built_at")
        .fetchall()
    )  # fmt: skip
    assert [(b, rid, u) for b, rid, u, *_ in env] == [(b, "77", url) for b in (b1, b2, b3)]
    for *_, dbt_version, versions, took in env:
        assert dbt_version and json.loads(versions)["dbt-core"] == dbt_version and took > 0
    # 'served' serves the same release again (the Snowflake migration re-exports it without a crawl).
    assert quality.rollback(Lake(), "served", "re-export for Snowflake migration") == b1
    assert current.read_bytes() == first_release
    # The next build is compared with what customers get now (b1), not the newest passing build (b2).
    monkeypatch.setattr(cli, "_today", lambda: "2026-10-26")
    cli.cmd_build(args("2026-10-26"))
    assert passed_vars[-1]["baseline_build"] == b1
    # Diffing the first build has nothing before it to compare with, never a later build.
    assert "error" in quality.build_diff(Lake(), new=b1)


def test_a_crashed_dbt_step_is_not_recorded_with_old_results(monkeypatch, tmp_path):
    from coffeedata import cli

    monkeypatch.setattr(cli, "DBT_TARGET", tmp_path)
    (tmp_path / "run_results.json").write_text('{"results": [{"unique_id": "test.x", "status": "pass"}]}')
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=2)
    )  # crashes early
    passed, results = cli.dbt_build(Lake(str(tmp_path / "lake")), tmp_path / "c.duckdb")
    assert passed is False and results == [{}, {}] and not (tmp_path / "manifest.json").exists()


def test_a_build_with_no_pass_verdict_is_never_a_baseline(lake, tmp_path):
    # One definition of passing everywhere (quality.builds, build-diff, the volume check): a build needs its "pass"
    # verdict row, so one whose record is incomplete never becomes the baseline.
    run(lake, "2026-10-26", RID, FULL)
    lake.write("builds", "builds/run_date=2026-10-19/build=b0.parquet", [
        {"build_id": "b0", "built_at": datetime(2026, 10, 20, tzinfo=UTC), "kind": "metric",
         "unique_id": "listed_coffees", "value": 400.0}])  # fmt: skip
    build(lake, tmp_path)  # 40 vs 400 would fail; b0 has no verdict, so there is no baseline
    assert [ok for _, _, ok in quality.builds(lake)] == [False]


def test_volume_metrics_count_the_current_catalog(lake, tmp_path):
    archived = [product(f"OLD{i}", available=False) | {"tags": ["Archived"]} for i in range(30)]
    run(lake, "2026-10-26", RID, FULL[:10] + archived)
    con = build(lake, tmp_path)
    metrics = dict(con.execute("select metric, value from mart_build_profile").fetchall())
    assert metrics["listed_coffees"] == 10  # a back catalog appearing isn't a swing in the market


def test_volume_metrics_leave_wholesale_out_like_the_site(lake, tmp_path):
    # The volume check's baseline counts what customers see: a roaster's wholesale twins aren't more coffees.
    wholesale = [product(f"W{i}", title=f"Wholesale - 5LB Coffee {i} Roast") for i in range(5)]
    run(lake, "2026-10-26", RID, FULL[:10] + wholesale)
    con = build(lake, tmp_path)
    metrics = dict(con.execute("select metric, value from mart_build_profile").fetchall())
    assert (metrics["listed_coffees"], metrics["in_stock_coffees"]) == (10, 10)


def test_a_first_week_use_batch_that_does_not_pin_is_no_override(lake, tmp_path):
    run(lake, "2026-10-19", RID, FULL)
    lake.write("run_overrides", "run_overrides/override_id=manual.parquet", [{
        "added_at": datetime(2026, 10, 20, tzinfo=UTC), "roaster_id": RID, "run_date": date(2026, 10, 19),
        "action": "use_batch", "batch_id": "no-such-batch", "reason": "typo"}])  # fmt: skip
    con = build(lake, tmp_path)  # same as in later weeks: the week counts as if there were no override
    assert con.execute("select counted from stg_run_quality").fetchone() == (True,)


def test_overrides_given_in_the_same_second_resolve_in_order(lake, tmp_path):
    run(lake, "2026-10-19", RID, FULL)
    run(lake, "2026-10-26", RID, FULL[:12])  # suspect
    quality.add_override(lake, RID, "2026-10-26", "reject", "first look: bad", None)
    quality.add_override(lake, RID, "2026-10-26", "accept", "second look: real", None)
    times = [
        r[0]
        for r in lake.connect()
        .execute(
            f"select added_at from {lake.scan('run_overrides')} where roaster_id is not null order by added_at"
        )
        .fetchall()
    ]
    assert len(times) == 2 and times[0] < times[1]
    con = build(lake, tmp_path)
    assert con.execute("select override_action, counted from stg_run_quality where run_date = '2026-10-26'"
                       ).fetchone() == ("accept", True)  # fmt: skip


def test_an_override_reaches_a_remote_lake(tmp_path, monkeypatch):
    from huggingface_hub import HfApi

    sent = []
    monkeypatch.setattr(HfApi, "upload_file", lambda self, **kw: sent.append(kw))
    remote = Lake("hf://datasets/someone/coffee", staging=tmp_path / "staging")
    from coffeedata.lake import GLOBS

    monkeypatch.setattr(remote, "sources", lambda table: [str(remote.root / GLOBS[table])])  # no network
    path = quality.add_override(remote, RID, "2026-10-26", "reject", "bad scrape", None)
    assert [s["path_in_repo"] for s in sent] == [path.relative_to(remote.root).as_posix()]
    assert sent[0]["repo_id"] == "someone/coffee" and not path.exists()  # sent alone, not via a full publish


def test_the_first_override_on_a_remote_lake_works_before_run_overrides_exists(tmp_path, monkeypatch):
    """Until a publish uploads its schema file, the remote lake has no run_overrides/ folder at all."""
    from huggingface_hub import HfApi

    sent = []
    monkeypatch.setattr(HfApi, "upload_file", lambda self, **kw: sent.append(kw))
    remote = Lake("hf://datasets/someone/coffee", staging=tmp_path / "staging")
    nowhere = str(tmp_path / "no-such-folder" / "*.parquet")  # stands in for the missing remote folder
    monkeypatch.setattr(remote, "sources", lambda table: [nowhere])
    path = quality.add_override(remote, RID, "2026-10-26", "reject", "bad scrape", None)
    assert [s["path_in_repo"] for s in sent] == [path.relative_to(remote.root).as_posix()]


def test_remote_releases_are_found_by_listing_only_the_releases_folder(monkeypatch):
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import RepoFile, RepoFolder

    asked = []

    def tree(self, repo_id, path_in_repo=None, recursive=False, repo_type=None):
        asked.append(path_in_repo)
        return [RepoFolder(path="releases/run_date=2026-10-26", oid="d"),
                RepoFile(path="releases/run_date=2026-10-26/build=b1/dim_coffee.parquet", size=1, oid="x")]  # fmt: skip

    monkeypatch.setattr(HfApi, "list_repo_tree", tree)
    quality._remote_releases.cache_clear()
    lake = Lake("hf://datasets/someone/coffee-x")
    assert quality.release_files(lake, "b1") == {
        "dim_coffee": "hf://datasets/someone/coffee-x/releases/run_date=2026-10-26/build=b1/dim_coffee.parquet"
    }
    assert asked == ["releases"]


def test_a_release_that_could_not_be_written_in_full_is_recorded_as_failed(lake, tmp_path):
    import duckdb

    db = tmp_path / "partial.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table dim_coffee as select 1 as x")  # every other release table is missing
    con.close()
    assert quality.record_build(lake, db, "2026-10-26", tmp_path / "none.json", [{}, {}], True, "bx") is False
    assert [ok for b, _, ok in quality.builds(lake) if b == "bx"] == [False]


def test_export_and_site_default_to_the_served_release(monkeypatch):
    from coffeedata import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_export_public", lambda a: seen.update({"export-public": a.db}))
    monkeypatch.setattr(cli, "cmd_site", lambda a: seen.update({"site": a.db}))
    cli.main(["export-public"])
    cli.main(["site"])
    assert seen == {"export-public": str(cli.SERVED_DB), "site": str(cli.SERVED_DB)}


def test_served_is_none_only_when_nothing_was_ever_served(tmp_path):
    lake = Lake(str(tmp_path / "lake"))
    assert quality.served(lake) is None  # no marts/release.parquet yet
    (tmp_path / "lake" / "marts").mkdir(parents=True)
    (tmp_path / "lake" / "marts" / "release.parquet").write_bytes(b"not parquet")
    with pytest.raises(
        Exception, match="(?i)parquet"
    ):  # any other failed read is raised, not "nothing served"
        quality.served(lake)


def test_builds_in_the_same_second_are_ordered_by_build_id(tmp_path):
    lake = Lake(str(tmp_path / "lake"))
    lake.ensure_schemas()
    at = datetime(2026, 10, 26, 12, tzinfo=UTC)
    for b in ("20261026T120000-bbbbbb", "20261026T120000-aaaaaa"):
        row = {
            "build_id": b,
            "run_date": date(2026, 10, 26),
            "built_at": at,
            "kind": "build",
            "status": "pass",
        }
        lake.write("builds", f"builds/run_date=2026-10-26/build={b}.parquet", [row])
    assert [b for b, _, _ in quality.builds(lake)] == ["20261026T120000-aaaaaa", "20261026T120000-bbbbbb"]


def test_remote_globs_only_for_folders_the_dataset_has(tmp_path, monkeypatch):
    from coffeedata import lake as lake_mod

    remote = Lake("hf://datasets/someone/coffee", staging=tmp_path / "staging")
    monkeypatch.setitem(lake_mod._REMOTE_FOLDERS, remote.url, frozenset({"fetches"}))
    assert any(s.startswith("hf://") for s in remote.sources("fetches"))
    assert not any(s.startswith("hf://") for s in remote.sources("builds"))  # not published yet: staging only
    monkeypatch.setitem(lake_mod._REMOTE_FOLDERS, remote.url, None)  # unknown: read it remotely, as before
    assert any(s.startswith("hf://") for s in remote.sources("builds"))


def test_the_site_from_the_served_release_needs_one(tmp_path, monkeypatch, capsys):
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    site = ["site", "--served", "--db", str(tmp_path / "s.duckdb"), "--out", str(tmp_path / "site")]
    with pytest.raises(SystemExit, match="nothing is served yet"):
        cli.main(site)
    # The site workflow asks to skip instead: it says why, exits 0 and builds nothing (so nothing is deployed).
    cli.main([*site, "--skip-if-unserved"])
    assert "nothing is served yet" in capsys.readouterr().err
    assert not (tmp_path / "site").exists() and not (tmp_path / "s.duckdb").exists()
