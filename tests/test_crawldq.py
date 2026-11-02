"""crawl-dq: the gate's own rules on the week's unmerged crawl output, a repull list, and systemic alerts."""

import json
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from coffeedata import cli, connectors, control, crawldq, ingest, registry
from coffeedata.lake import Lake
from coffeedata.models import Roaster

from .lakehelp import product, run

WEEK = "2026-11-02"
HISTORY = ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"]
SHOPS = [f"s{i:02d}.com" for i in range(50)]


def _state(rid: str, platform: str = "shopify") -> registry.RoasterState:
    return registry.RoasterState(
        Roaster(roaster_id=rid, name=rid, host=rid, platform=platform), "active", None
    )


def _week(tmp_path, monkeypatch, failing: int = 0) -> Lake:
    """A lake with 4 counted weeks of 50 Shopify roasters (s00 lists 30 products, the rest 1) and this week's
    Shopify crawl in its unmerged partition: s00..s29 reached, s30..s49 left for the resume (a deadline). Among
    the reached: s00 came back with 5 products (suspect), s01 failed, s02 succeeded with nothing; with `failing`,
    that many more (from s03) failed too."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(crawldq, "DIR", tmp_path / "dq")
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state(r) for r in SHOPS])
    for d in HISTORY:
        for rid in SHOPS:
            run(lk, d, rid, [product(f"{rid}-{n}") for n in range(30 if rid == "s00.com" else 1)])
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    roasters = [_state(r).roaster for r in SHOPS]
    part.write_partition("run_manifests", WEEK, "m1", ingest.manifest_rows(roasters, date(2026, 11, 2), "m1",
                                                                           mode="full"))  # fmt: skip
    for i, rid in enumerate(SHOPS[:30]):
        if i == 0:
            run(part, WEEK, rid, [product(f"{rid}-{n}") for n in range(5)])
        elif i == 1 or 3 <= i < 3 + failing:
            run(part, WEEK, rid, None)
        elif i == 2:
            run(part, WEEK, rid, [])
        else:
            run(part, WEEK, rid, [product(f"{rid}-0")])
    control.finish(lk, control.start(lk, "crawl", WEEK, "shopify"), "deadline", skipped=20)
    return lk


def _files(lk: Lake, folder: str) -> list[str]:
    return sorted(
        str(p.relative_to(lk.root)) for p in (lk.root / folder).rglob("*.parquet") if "schema" not in p.name
    )


def test_a_finished_pass_is_judged_with_the_gates_rules_and_lists_what_to_fetch_again(tmp_path, monkeypatch):
    lk = _week(tmp_path, monkeypatch)
    before = {
        t: _files(lk, t) for t in ("fetches", "snapshots", "builds", "releases", "marts", "serving_log")
    }
    out = crawldq.run(lk, "2026-11-03")
    assert out["status"] == "checked" and out["systemic"] == []
    # attempted_only: the 20 roasters the pass left for its resume aren't failures yet (27 of 30 reached are ok;
    # over all 50 the share would be 54%, under run_ok_rate's 90%).
    assert out["tests"] == {
        "run_ok_rate": "pass",
        "adapter_ok_rate": "pass",
        "suspect_runs_need_review": "warn",
    }
    assert [(e["roaster_id"], e["reasons"]) for e in out["repull"]] == [
        ("s00.com", ["suspect"]),
        ("s01.com", ["failed"]),
        ("s02.com", ["empty"]),
    ]
    assert out["repull"][0]["products"] == 5 and out["repull"][0]["baseline_products"] == 30
    # Never a build, a release, a served mart or a merge: the lake's tables are as they were.
    assert {t: _files(lk, t) for t in before} == before
    assert list((lk.root / "raw").rglob("*.parquet"))  # the partition is untouched: no compaction, no merge
    # Its one write: the job_manifests rows (job dq), whose detail is the verdict.
    dq = [r for r in control.rows(lk, WEEK) if r["job"] == "dq"]
    assert [r["status"] for r in dq] == ["done"]
    assert json.loads(dq[0]["detail"])["repull"] == out["repull"]
    # The next poll has nothing new to judge.
    assert crawldq.run(lk, WEEK)["status"].startswith("up to date")

    # The resume slot fetches the reached-but-bad roasters again, with the ones it never reached.
    seen = {}
    monkeypatch.setattr(ingest, "run", lambda lake, states, *a, **k: seen.setdefault("ids", [
        s.roaster.roaster_id for s in states]) and {"roasters": 0, "by_adapter": {}, "skipped_deadline": 0})  # fmt: skip
    connectors.crawl(lk, "shopify", WEEK)
    assert {"s00.com", "s01.com", "s02.com"} <= set(seen["ids"]) and "s03.com" not in seen["ids"]
    assert set(SHOPS[30:]) <= set(seen["ids"])

    # The re-fetch is a new batch of the same week, and the week's newest ok batch is the one dbt counts
    # (stg_run_walk; only a use_batch override would pin another): s00's good re-fetch replaces its suspect one,
    # and the next check no longer lists it.
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    redone = run(part, WEEK, "s00.com", [product(f"s00.com-{n}") for n in range(30)], hour=20)
    control.finish(lk, control.start(lk, "crawl", WEEK, "shopify"), "done")
    out = crawldq.run(lk, WEEK)
    assert out["status"] == "checked" and "s00.com" not in [e["roaster_id"] for e in out["repull"]]
    con = duckdb.connect(str(tmp_path / "dq" / "dq.duckdb"), read_only=True)
    assert con.execute(
        "select batch_id, suspect, counted from stg_run_quality where roaster_id = 's00.com' and run_date = ?",
        [date(2026, 11, 2)],
    ).fetchall() == [(redone, False, True)]


def test_a_platform_below_the_gates_floor_is_systemic_and_held_off_the_repull_list(
    tmp_path, monkeypatch, capsys
):
    _week(
        tmp_path, monkeypatch, failing=20
    )  # 21 of the 30 reached failed, 1 empty: 27% ok, under adapter_ok_rate's 50%
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl-dq", "--run-date", WEEK])
    assert "systemic" in str(e.value)
    printed = capsys.readouterr().out
    alert = json.loads(next(line for line in printed.splitlines() if '"severity": "ERROR"' in line))
    assert alert["systemic"] == [
        {
            "scope": "connector",
            "connector": "shopify",
            "rule": "adapter_ok_rate",
            "roasters": 30,
            "ok_share": 0.267,
        }
    ]
    detail = json.loads(next(r for r in control.rows(Lake(), WEEK) if r["job"] == "dq")["detail"])
    assert detail["repull"] == [] and detail["held_back"] == 23  # 21 failed, 1 empty, 1 suspect


def _db(tmp_path, completeness: list[tuple], failing: dict[str, list[tuple]]):
    """A scratch database as dbt leaves it: run_completeness and the stored failures of the gate's tests."""
    db = tmp_path / "dq.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "create table run_completeness (roaster_id varchar, adapter varchar, attempted bool, ok bool, "
        "has_catalog bool, catalog_ok bool)"
    )
    if completeness:
        con.executemany("insert into run_completeness values (?, ?, ?, ?, ?, ?)", completeness)
    con.execute("create schema main_dbt_test__audit")
    cols = {
        "adapter_ok_rate": "adapter varchar, roasters bigint, ok_share double",
        "run_ok_rate": "run_date date, roasters bigint, ok_share double",
        "suspect_runs_need_review": "roaster_id varchar, run_date date, batch_id varchar, products bigint, "
        "baseline_products double",
    }
    for test, rows in failing.items():
        con.execute(f"create table main_dbt_test__audit.{test} ({cols[test]})")
        if rows:
            con.executemany(
                f"insert into main_dbt_test__audit.{test} values ({', '.join('?' * len(rows[0]))})", rows
            )
    con.close()
    return db


def _results(**statuses) -> list[dict]:
    tests = {"run_ok_rate": "pass", "adapter_ok_rate": "pass", "suspect_runs_need_review": "pass"} | statuses
    return [
        {"results": [{"unique_id": "model.coffeedata.run_completeness", "status": "success"}]},
        {"results": [{"unique_id": f"test.coffeedata.{t}", "status": s} for t, s in tests.items()]},
    ]


def test_two_platforms_down_at_once_point_at_the_image_and_hold_every_repull(tmp_path):
    rc = [("a.com", "shopify", True, False, True, False), ("b.com", "woocommerce", True, False, True, False),
          ("c.com", "generic", True, False, True, False)]  # fmt: skip
    db = _db(tmp_path, rc, {"adapter_ok_rate": [("shopify", 40, 0.1), ("woocommerce", 30, 0.2)],
                            "run_ok_rate": [(date(2026, 11, 2), 70, 0.14)]})  # fmt: skip
    out = crawldq.judge(db, _results(adapter_ok_rate="fail", run_ok_rate="fail"))
    assert [s["scope"] for s in out["systemic"]] == ["connector", "connector", "image"]
    assert out["repull"] == [] and out["held_back"] == 3  # generic's too: the image is shared


def test_the_run_below_its_floor_with_no_platform_to_blame_is_systemic(tmp_path):
    db = _db(tmp_path, [("a.com", "generic", True, True, True, False)], {"run_ok_rate": [(date(2026, 11, 2), 30,
                                                                                          0.8)]})  # fmt: skip
    out = crawldq.judge(db, _results(run_ok_rate="fail"))
    assert out["systemic"] == [{"scope": "run", "rule": "run_ok_rate", "roasters": 30, "ok_share": 0.8}]
    assert out["repull"] == [{"roaster_id": "a.com", "adapter": "generic", "reasons": ["empty"]}]
    # While a connector still crawls, it is left out, and the run as a whole isn't judged yet.
    out = crawldq.judge(db, _results(run_ok_rate="fail"), running=frozenset({"generic"}))
    assert out["systemic"] == [] and out["repull"] == [] and out["running"] == ["generic"]


def test_a_dbt_error_is_no_verdict_not_bad_data(tmp_path):
    db = _db(tmp_path, [], {})
    with pytest.raises(crawldq.CheckError, match="run_completeness"):
        crawldq.judge(
            db, [{"results": [{"unique_id": "model.coffeedata.run_completeness", "status": "error"}]}, {}]
        )
    with pytest.raises(crawldq.CheckError):  # a renamed test that no longer runs
        crawldq.judge(db, _results()[:1] + [{"results": []}])
    with pytest.raises(crawldq.CheckError, match="adapter_ok_rate says fail but stored 0"):  # never evaluated
        crawldq.judge(db, _results(adapter_ok_rate="fail"))


def test_a_poll_judges_only_when_a_crawl_attempt_has_ended_since_the_last_check(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(crawldq, "DIR", tmp_path / "dq")
    judged = []
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (True, []))
    monkeypatch.setattr(crawldq, "judge", lambda db, results, running=frozenset(): judged.append(running) or {
        "systemic": [], "repull": [], "held_back": 0, "by_connector": [], "running": sorted(running)})  # fmt: skip
    assert crawldq.run(lk, WEEK)["status"].startswith("waiting")  # no crawl yet
    shop = control.start(lk, "crawl", WEEK, "shopify")
    assert crawldq.run(lk, WEEK)["status"].startswith("waiting")  # running, nothing ended
    control.finish(lk, control.start(lk, "crawl", WEEK, "woocommerce"), "done")
    assert crawldq.run(lk, WEEK)["status"] == "checked" and judged == [frozenset({"shopify"})]
    assert crawldq.run(lk, WEEK)["status"].startswith("up to date")
    control.finish(lk, shop, "deadline")
    assert crawldq.run(lk, WEEK)["status"] == "checked" and judged[-1] == frozenset()
    # A confirmation arm's finished crawl is not a pass of the week: nothing new to judge.
    control.finish(lk, control.start(lk, "crawl", WEEK, "squarespace"), "done", detail={"slice": "0/2"})
    assert crawldq.run(lk, WEEK)["status"].startswith("up to date")


def test_a_check_that_could_not_run_alerts_once_not_on_every_poll(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(crawldq, "DIR", tmp_path / "dq")
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (False, [{}, {}]))  # dbt crashed
    control.finish(lk, control.start(lk, "crawl", WEEK, "woocommerce"), "done")
    with pytest.raises(crawldq.CheckError):
        crawldq.run(lk, WEEK)
    assert [r["status"] for r in control.rows(lk, WEEK) if r["job"] == "dq"] == ["failed"]
    assert crawldq.run(lk, WEEK)["status"].startswith("up to date")
    with pytest.raises(crawldq.CheckError):  # --force tries again
        crawldq.run(lk, WEEK, force=True)


def test_a_crawl_that_died_counts_as_ended(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(crawldq, "DIR", tmp_path / "dq")
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (True, []))
    monkeypatch.setattr(crawldq, "judge", lambda db, results, running=frozenset(): {
        "systemic": [], "repull": [], "held_back": 0, "by_connector": [], "running": sorted(running)})  # fmt: skip
    # Only its running row, started longer ago than the jobs' time limit: killed or timed out.
    old = control.start(lk, "crawl", WEEK, "generic")
    control.write(lk, old | {"started_at": old["started_at"] - crawldq.JOB_LIMIT - timedelta(minutes=1)})
    out = crawldq.run(lk, WEEK)
    assert out["status"] == "checked" and out["died"] == ["generic"] and out["running"] == []


def test_a_repull_is_asked_once_from_the_latest_check_that_judged_its_connector(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    clock = iter(datetime(2026, 11, 2, 18, tzinfo=UTC) + timedelta(minutes=i) for i in range(100))
    monkeypatch.setattr(control, "now", lambda: next(clock))  # the checks ran from 18:00, a minute apart
    monkeypatch.setattr(
        registry,
        "current",
        lambda lake, *a, **k: [
            _state("a.com"),
            _state("b.com"),
            _state("c.com"),
            _state("w.com", "woocommerce"),
        ],
    )

    def check(repull, running=()):
        job = control.start(lk, "dq", WEEK)
        control.finish(lk, job, "done", detail={"repull": repull, "running": list(running)})

    check([{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["suspect"]},
           {"roaster_id": "b.com", "adapter": "shopify", "reasons": ["empty"]},
           {"roaster_id": "c.com", "adapter": "shopify", "reasons": ["failed"]},  # every crawl pass asks it anyway
           {"roaster_id": "w.com", "adapter": "woocommerce", "reasons": ["suspect"]}])  # fmt: skip
    # A later check that ran while Shopify still crawled said nothing about it: for Shopify the earlier one holds;
    # for WooCommerce the later one does (it found the same store still bad).
    check([{"roaster_id": "w.com", "adapter": "woocommerce", "reasons": ["suspect"]}], running=["shopify"])
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    ids = lambda: sorted(s.roaster.roaster_id for s in crawldq.repull_states(part, "shopify", WEEK))  # noqa: E731
    assert ids() == ["a.com", "b.com"]
    # Fetched again since the check (the resume slot did it): not asked a third time.
    run(part, WEEK, "a.com", [product("x")], hour=23)
    assert ids() == ["b.com"]
    assert [s.roaster.roaster_id for s in crawldq.repull_states(lk, "woocommerce", WEEK)] == ["w.com"]


def test_crawl_retry_also_fetches_the_repull_list_but_not_the_failed(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com"), _state("f.com")])
    control.finish(lk, control.start(lk, "dq", WEEK), "done", detail={"repull": [
        {"roaster_id": "a.com", "adapter": "shopify", "reasons": ["empty"]},
        {"roaster_id": "f.com", "adapter": "shopify", "reasons": ["failed"]}]})  # fmt: skip
    seen = {}
    monkeypatch.setattr(ingest, "run", lambda lake, states, *a, **k: seen.setdefault("ids", [
        s.roaster.roaster_id for s in states]) and {"by_adapter": {}})  # fmt: skip
    cli.main(["crawl-retry", "--connector", "shopify", "--run-date", WEEK])
    assert seen["ids"] == ["a.com"]
