"""Data quality: suspect runs held out of history, overrides, the build-to-build volume and freshness checks, and
the build records and mart history that `coffeedata build-diff` compares."""

import argparse
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

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


def _baseline(lake, listed: float):
    now = datetime(2026, 10, 20, tzinfo=UTC)
    lake.write("builds", "builds/run_date=2026-10-19/build=b1.parquet", [
        {"build_id": "b1", "built_at": now, "kind": "build", "unique_id": "build", "status": "pass"},
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
        {"build_id": "b2", "built_at": later, "kind": "build", "unique_id": "build", "status": "fail"},
        {"build_id": "b2", "built_at": later, "kind": "node", "unique_id": "test.y", "status": "fail"},
        {"build_id": "b2", "built_at": later, "kind": "metric", "unique_id": "listed_coffees", "value": 5.0},
    ])  # fmt: skip
    build(lake, tmp_path)  # compared with b1 (40), not the failed b2 (5)


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


def test_releases_are_recorded_served_gated_and_rolled_back(lake, monkeypatch, tmp_path):
    from coffeedata import cli

    real_dbt_build = cli.dbt_build
    # The real seed list has ~2,700 roasters this lake never fetches; completeness has its own test above.
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real_dbt_build(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0}))  # fmt: skip
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

    def args(d):
        return argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date=d, accept_volume_change=False)

    current = lake.root / "marts" / "dim_coffee.parquet"
    run(lake, "2026-10-19", RID, FULL[:30])
    monkeypatch.setattr(cli, "_today", lambda: "2026-10-19")  # each build on its run's day: fresh
    assert cli.cmd_build(args("2026-10-19")) is True
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
    assert cli.cmd_build(args("2026-10-26")) is True
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
    assert quality.rollback(Lake(), reason="bad prices from one roaster") == b1
    assert current.read_bytes() == first_release
    assert quality.served(Lake())["build_id"] == b1
    assert quality.served(Lake())["reason"] == "bad prices from one roaster"
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
