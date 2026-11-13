"""The crawl success SLO (#96): dbt's crawl_slo_roasters and mart_crawl_slo over synthetic lakes (the definition's
edge cases are dbt unit tests in dbt/models/crawl_slo.yml), and the production health check's warnings on it
(coffeedata.prod_health, config/prod_health.toml). A benchmark: it warns, and never fails a build."""

import tomllib
from datetime import date
from pathlib import Path

import duckdb
import pytest
import yaml

from coffeedata import prod_health as ph
from coffeedata import triage

from .lakehelp import needs_dbt, product, run
from .test_dbt import build
from .test_triage import FakeGitHub

ROOT = Path(__file__).resolve().parents[1]
# The fixture registry (tests/fixtures/roasters.csv): six roasters, none opted out, each with a platform.
STORES = {
    "onyxcoffeelab.com": "shopify",
    "counterculturecoffee.com": "shopify",
    "intelligentsia.com": "shopify",
    "anaheimcoffee.com": "woocommerce",
    "vividcoffee.com": "squarespace",
    "1802roasters.com": "generic",
}
SLO_TABLES = ("crawl_slo_roasters", "mart_crawl_slo")


def _week(lake, run_date, failed=(), closed=()):
    for store, adapter in STORES.items():
        if store in closed:
            run(lake, run_date, store, None, adapter=adapter, status="unavailable")
        else:
            run(lake, run_date, store, None if store in failed else [product("A")], adapter=adapter)


def _rows(con, table, where="true"):
    return con.execute(f"select * from {table} where {where} order by all").fetchall()


def _slo(con, platform="all"):
    cols = "run_date, roasters, ok, met, burn_rate, window_runs, window_failed, budget_remaining"
    return con.execute(
        f"select {cols} from mart_crawl_slo where platform = ? order by run_date", [platform]
    ).fetchall()


@needs_dbt
def test_each_run_has_its_share_and_the_budget_of_the_window_that_ends_at_it(lake, tmp_path):
    """Three weekly runs of six roasters: all fetched; one failed; one found closed (which the gate's ok rates leave
    out and the SLO counts). Six roasters allow 0.06 failures a run, so one failure burns at 16.67 times the rate.
    The build records its own run's row in mart_build_profile, where the production health check reads it."""
    _week(lake, "2026-10-19")
    _week(lake, "2026-10-26", failed=["anaheimcoffee.com"])
    _week(lake, "2026-11-02", closed=["vividcoffee.com"])
    con = build(lake, tmp_path)
    assert _slo(con) == [
        (date(2026, 10, 19), 6, 6, True, 0.0, 1, 0, 1.0),
        (date(2026, 10, 26), 6, 5, False, 16.666667, 2, 1, -7.333333),
        (date(2026, 11, 2), 6, 5, False, 16.666667, 3, 2, -10.111111),
    ]
    assert _slo(con, "woocommerce") == [
        (date(2026, 10, 19), 1, 1, True, 0.0, 1, 0, 1.0),
        (date(2026, 10, 26), 1, 0, False, 100.0, 2, 1, -49.0),
        (date(2026, 11, 2), 1, 1, True, 0.0, 3, 1, -32.333333),
    ]
    assert con.execute(
        "select outcome from crawl_slo_roasters where run_date = '2026-11-02' and roaster_id = 'vividcoffee.com'"
    ).fetchone() == ("unavailable",)
    profile = dict(
        con.execute("select metric, value from mart_build_profile where metric like 'crawl_slo%'").fetchall()
    )
    assert profile == {
        "crawl_slo_roasters": 6.0, "crawl_slo_ok": 5.0, "crawl_slo_share": 0.833333, "crawl_slo_met": 0.0,
        "crawl_slo_burn_rate": 16.666667, "crawl_slo_budget_remaining": -10.111111, "crawl_slo_window_runs": 3.0,
        "crawl_slo_roasters:shopify": 3.0, "crawl_slo_ok:shopify": 3.0, "crawl_slo_share:shopify": 1.0,
        "crawl_slo_met:shopify": 1.0,
        "crawl_slo_roasters:woocommerce": 1.0, "crawl_slo_ok:woocommerce": 1.0, "crawl_slo_share:woocommerce": 1.0,
        "crawl_slo_met:woocommerce": 1.0,
        "crawl_slo_roasters:squarespace": 1.0, "crawl_slo_ok:squarespace": 0.0, "crawl_slo_share:squarespace": 0.0,
        "crawl_slo_met:squarespace": 0.0,
        "crawl_slo_roasters:generic": 1.0, "crawl_slo_ok:generic": 1.0, "crawl_slo_share:generic": 1.0,
        "crawl_slo_met:generic": 1.0,
    }  # fmt: skip
    # One meaning of "crawled": every roaster run_completeness expects in the run is in the SLO's population for
    # it, with the same ok and the same connector.
    assert con.execute(
        "select count(*), count(s.roaster_id), bool_and(s.ok = c.ok and s.platform = c.adapter and s.attempted = "
        "c.attempted) from run_completeness c left join crawl_slo_roasters s using (run_date, roaster_id)"
    ).fetchone() == (6, 6, True)


@needs_dbt
def test_the_same_lake_builds_the_same_slo_twice(lake, tmp_path):
    """Idempotent: two builds of the same raw give identical rows, in the SLO's tables and in what the build
    records of it. Nothing in them reads the clock or an earlier build."""
    _week(lake, "2026-10-19")
    _week(lake, "2026-10-26", failed=["anaheimcoffee.com", "1802roasters.com"])
    _week(lake, "2026-11-02", closed=["vividcoffee.com"])
    first = build(lake, tmp_path / "one")
    second = build(lake, tmp_path / "two")
    for table in SLO_TABLES:
        assert _rows(first, table) and _rows(first, table) == _rows(second, table), table
    recorded = "metric like 'crawl_slo%'"
    assert _rows(first, "mart_build_profile", recorded) == _rows(second, "mart_build_profile", recorded)


@needs_dbt
def test_an_older_run_built_again_gives_its_numbers_again(lake, tmp_path):
    """The window is anchored on the run being built, not on the day of the build or the newest data: with a later
    run stored (and a build of it recorded in between), building 2026-10-26 again gives the rows it gave before,
    and the later run's build keeps the earlier runs' rows and adds its own."""
    _week(lake, "2026-10-19")
    _week(
        lake, "2026-10-26", closed=["anaheimcoffee.com"]
    )  # closed, not failed: the gate (on with as_of_run) passes
    then = build(lake, tmp_path / "then", as_of_run="2026-10-26")
    before = {t: _rows(then, t) for t in SLO_TABLES}
    recorded = _rows(then, "mart_build_profile", "metric like 'crawl_slo%'")
    assert [r[0] for r in _slo(then)] == [date(2026, 10, 19), date(2026, 10, 26)]
    _week(lake, "2026-11-02", closed=["onyxcoffeelab.com", "intelligentsia.com"])
    # The gate refuses to release an older run over a lake that holds a newer one (public_coffee_is_fresh), so this
    # build exits non-zero; its models are built all the same, and those are what is compared.
    build(lake, tmp_path / "again", check=False, as_of_run="2026-10-26")
    again = duckdb.connect(str(tmp_path / "again" / "coffee.duckdb"), read_only=True)
    assert {t: _rows(again, t) for t in SLO_TABLES} == before
    assert _rows(again, "mart_build_profile", "metric like 'crawl_slo%'") == recorded
    latest = build(lake, tmp_path / "latest", as_of_run="2026-11-02")
    for t in SLO_TABLES:
        assert _rows(latest, t, "run_date <= '2026-10-26'") == before[t], t
    assert _slo(latest)[-1][:4] == (date(2026, 11, 2), 6, 4, False)


@needs_dbt
def test_a_run_under_the_target_still_builds(lake, tmp_path):
    """A benchmark, not a gate (the owner's decision in #96): with the gate's own checks on (as_of_run), a run that
    misses the SLO builds and passes. The closed store is left out of run_ok_rate, and counts against the SLO."""
    _week(lake, "2026-10-19")
    _week(lake, "2026-10-26", closed=["vividcoffee.com"])
    con = build(lake, tmp_path, as_of_run="2026-10-26")  # asserts dbt's exit code is 0
    assert _slo(con)[-1][:4] == (date(2026, 10, 26), 6, 5, False)
    assert con.execute("select avg(catalog_ok::int) from run_completeness where has_catalog").fetchone() == (
        1.0,
    )


# --- the production health check's warnings -------------------------------------------------------------------

MET = {"build:crawl_slo_met": 1.0, "build:crawl_slo_burn_rate": 0.4, "build:crawl_slo_budget_remaining": 0.7}
ISSUE_WEEK = {  # the week #96 records: 2,424 of 2,540, a burn rate of 4.57
    "build:crawl_slo_met": 0.0,
    "build:crawl_slo_burn_rate": 4.566929,
    "build:crawl_slo_budget_remaining": -3.566929,
}
SERVED = {"served": {"build_id": "b2", "run_date": "2026-11-09", "previous": None}}


def _statuses(metrics):
    results = ph.judge(metrics, ph.load_config())
    return {
        r["metric"]: r["status"]
        for r in results
        if r["rule"] == "crawl-slo" and r["status"] != "not_measured"
    }


@pytest.mark.parametrize(
    "metrics,expected",
    [
        (MET, {"met": "ok", "burn_rate": "ok", "budget_remaining": "ok"}),
        # this run met the target, and the window is still overspent by an earlier one
        (MET | {"build:crawl_slo_budget_remaining": -0.25}, {"met": "ok", "burn_rate": "ok", "budget_remaining": "warning"}),
        # a miss that leaves budget: 98.5% after three clean weeks
        ({"build:crawl_slo_met": 0.0, "build:crawl_slo_burn_rate": 1.5, "build:crawl_slo_budget_remaining": 0.625},
         {"met": "warning", "burn_rate": "ok", "budget_remaining": "ok"}),
        # exactly a window's budget in one run is not yet past the bound; a little more is
        ({"build:crawl_slo_met": 0.0, "build:crawl_slo_burn_rate": 4.0, "build:crawl_slo_budget_remaining": 0.0},
         {"met": "warning", "burn_rate": "ok", "budget_remaining": "ok"}),
        (ISSUE_WEEK, {"met": "warning", "burn_rate": "warning", "budget_remaining": "warning"}),
        # the worst there is: nothing fetched. Still a warning: the SLO never pages as critical
        ({"build:crawl_slo_met": 0.0, "build:crawl_slo_burn_rate": 100.0, "build:crawl_slo_budget_remaining": -99.0},
         {"met": "warning", "burn_rate": "warning", "budget_remaining": "warning"}),
    ],
)  # fmt: skip
def test_the_checked_in_bounds_warn_below_the_target_and_never_more(metrics, expected):
    assert _statuses(metrics) == {f"build:crawl_slo_{k}": v for k, v in expected.items()}


def test_the_slo_has_no_critical_bound_and_its_fast_burn_is_dbts_window():
    slo = [m for m in ph.load_config()["metric"] if m["rule"] == "crawl-slo"]
    assert {m["name"] for m in slo} == set(ph.SLO_BOUNDS) | {ph.SLO_REPORTED + p for p in ph.SLO_PLATFORMS}
    assert not [k for m in slo for k in m if k.startswith("critical")]
    dbt_vars = yaml.safe_load((ROOT / "dbt/dbt_project.yml").read_text())["vars"]
    assert dbt_vars["crawl_slo_window_runs"] == ph.SLO_FAST_BURN and dbt_vars["crawl_slo_target"] == 0.99
    assert (
        next(m for m in slo if m["name"] == "build:crawl_slo_burn_rate")["warning_above"] == ph.SLO_FAST_BURN
    )


def test_a_miss_opens_one_warning_issue_that_names_the_platforms_and_no_value():
    gh = FakeGitHub()
    platforms = {"build:crawl_slo_met:shopify": 1.0, "build:crawl_slo_met:woocommerce": 0.0,
                 "build:crawl_slo_met:generic": 0.0}  # fmt: skip
    results = ph.judge(ISSUE_WEEK | platforms, ph.load_config())
    alert = next(a for a in ph.alerts(SERVED, results, "R1", "s1") if a["key"] == "prod:crawl-slo")
    assert alert["firing"] and alert["severity"] == "warning" and "3 metric(s)" in alert["summary"]
    assert triage.sync([alert], gh, now="t") == [("opened", "prod:crawl-slo", 1)]
    body = gh.store[0]["body"]
    assert set(gh.store[0]["labels"]) == {"triage", "prod-health", "severity:warning"}
    assert "| all | `build:crawl_slo_met` | warning | below 100.00% |" in body
    assert "| all | `build:crawl_slo_burn_rate` | warning | above 4 |" in body
    assert "| all | `build:crawl_slo_budget_remaining` | warning | below 0.00% |" in body
    assert "| woocommerce | `build:crawl_slo_met:woocommerce` | report only | below 100.00% |" in body
    assert "| generic | `build:crawl_slo_met:generic` | report only |" in body and "shopify" not in body
    assert "4.5" not in body and "3.5" not in body  # the lake is private: no measured value
    # the run summary marks them too
    assert "`build:crawl_slo_met:generic` | < 1 | report only" in ph.summary(SERVED, results)
    # recovery closes it, though a small platform is still under the target by itself
    good = ph.judge(MET | platforms, ph.load_config())
    recovered = next(a for a in ph.alerts(SERVED, good, "R2", "s2") if a["key"] == "prod:crawl-slo")
    assert not recovered["firing"]
    assert triage.sync([recovered], gh, now="t") == [("closed", "prod:crawl-slo", 1)]


def test_a_platform_under_the_target_alone_opens_nothing():
    gh = FakeGitHub()
    results = ph.judge(MET | {"build:crawl_slo_met:generic": 0.0}, ph.load_config())
    assert _statuses(MET | {"build:crawl_slo_met:generic": 0.0})["build:crawl_slo_met:generic"] == "warning"
    alert = next(a for a in ph.alerts(SERVED, results, "R1", "s1") if a["key"] == "prod:crawl-slo")
    assert not alert["firing"] and triage.sync([alert], gh, now="t") == []


def test_the_build_record_carries_the_slo_and_a_build_from_before_it_measures_nothing(tmp_path):
    path = tmp_path / "builds" / "run_date=2026-11-09" / "build=b2.parquet"
    path.parent.mkdir(parents=True)
    rows = [("b1", "build", "build", "pass", None), ("b1", "metric", "run_ok_share", None, 0.95),
            ("b2", "build", "build", "pass", None), ("b2", "metric", "crawl_slo_met", None, 0.0),
            ("b2", "metric", "crawl_slo_burn_rate", None, 4.566929), ("b2", "metric", "crawl_slo_share", None, 0.954331),
            ("b2", "metric", "crawl_slo_budget_remaining", None, -3.566929),
            ("b2", "metric", "crawl_slo_met:generic", None, 0.0), ("b2", "metric", "crawl_slo_roasters", None, 2540.0)]  # fmt: skip
    con = duckdb.connect()
    con.execute(
        "create table t (build_id varchar, kind varchar, unique_id varchar, status varchar, value double)"
    )
    con.executemany("insert into t values (?, ?, ?, ?, ?)", rows)
    con.execute("alter table t add column resource_type varchar")
    con.execute("alter table t add column failures bigint")
    con.execute(f"copy t to '{path}' (format parquet)")
    got = ph.build_metrics([path], "b2")
    assert got == ISSUE_WEEK | {"build:crawl_slo_met:generic": 0.0, "build:passed": 1.0}
    old = ph.build_metrics([path], "b1")
    assert not [k for k in old if "crawl_slo" in k]
    assert _statuses(old) == {}  # not measured: no warning, and nothing to recover


def test_regenerating_the_thresholds_keeps_the_slos_fixed_bounds(tmp_path):
    """`prod_health baseline` writes the SLO's entries whatever the history holds: the checked-in ones, exactly."""
    release = tmp_path / "dim_coffee.parquet"
    duckdb.connect().execute(
        f"copy (select 'r:1' as coffee_id, 'shopify' as platform, true as is_listed, 'Kenya' as origin_country) "
        f"to '{release}' (format parquet)"
    )
    _, text, _ = ph.baseline([release], [], [], "2026-11-13", ["w0"])
    made = {m["name"]: m for m in tomllib.loads(text)["metric"] if m["rule"] == "crawl-slo"}
    checked_in = {m["name"]: m for m in ph.load_config()["metric"] if m["rule"] == "crawl-slo"}
    assert made == checked_in and len(made) == 8
    assert made["build:crawl_slo_met:generic"]["report_only"] is True
