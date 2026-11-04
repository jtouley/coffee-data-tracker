"""fetch-health: the gate's own fetch rules on the week's unmerged crawl output, its list, and systemic alerts. It
judges whether each fetch returned a catalog of about the right size, never whether the data is good."""

import json
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from coffeedata import cli, connectors, control, fetchhealth, ingest, registry
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
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
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
    out = fetchhealth.run(lk, "2026-11-03")
    assert out["status"] == "checked" and out["systemic"] == []
    # attempted_only: the 20 roasters the pass left for its resume aren't failures yet (27 of 30 reached are ok;
    # over all 50 the share would be 54%, under run_ok_rate's 90%).
    assert out["tests"] == {
        "run_ok_rate": "pass",
        "adapter_ok_rate": "pass",
        "suspect_runs_need_review": "warn",
        "edge_challenge_share": "pass",  # the check's own: no challenge here
    }
    assert [(e["roaster_id"], e["reasons"], e["rules"]) for e in out["repull"]] == [
        ("s00.com", ["suspect"], ["suspect_runs_need_review"]),
        ("s01.com", ["failed"], ["run_ok_rate"]),
        ("s02.com", ["empty"], ["run_ok_rate"]),
    ]
    assert out["repull"][0]["products"] == 5 and out["repull"][0]["baseline_products"] == 30
    # Never a build, a release, a served mart or a merge: the lake's tables are as they were.
    assert {t: _files(lk, t) for t in before} == before
    assert list((lk.root / "raw").rglob("*.parquet"))  # the partition is untouched: no compaction, no merge
    # Its one write: the job_manifests rows (job fetch-health), whose detail is the verdict.
    dq = [r for r in control.rows(lk, WEEK) if r["job"] == "fetch-health"]
    assert [r["status"] for r in dq] == ["done"]
    assert json.loads(dq[0]["detail"])["repull"] == out["repull"]
    # The next poll has nothing new to judge.
    assert fetchhealth.run(lk, WEEK)["status"].startswith("up to date")

    # With re-fetch on, the resume slot fetches the listed (reached, empty or short) roasters again, with the ones it never reached.
    monkeypatch.setenv(fetchhealth.REFETCH_ENV, "1")
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
    out = fetchhealth.run(lk, WEEK)
    assert out["status"] == "checked" and "s00.com" not in [e["roaster_id"] for e in out["repull"]]
    con = duckdb.connect(str(tmp_path / "fh" / "dq.duckdb"), read_only=True)
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
        cli.main(["fetch-health", "--run-date", WEEK])
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
    detail = json.loads(next(r for r in control.rows(Lake(), WEEK) if r["job"] == "fetch-health")["detail"])
    assert detail["repull"] == [] and detail["held_back"] == 23  # 21 failed, 1 empty, 1 suspect


def _db(tmp_path, completeness: list[tuple], failing: dict[str, list[tuple]]):
    """A scratch database as dbt leaves it: run_completeness and the stored failures of the check's tests."""
    db = tmp_path / "dq.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "create table run_completeness (roaster_id varchar, adapter varchar, attempted bool, ok bool, "
        "has_catalog bool, catalog_ok bool, quarantined bool, partial bool, partial_reason varchar, "
        "challenge varchar)"
    )
    # Rows of 6 (the gate's columns) get main's other fetch states unset: not quarantined, not partial, no challenge.
    rows = [(*r, False, False, None, None) if len(r) == 6 else r for r in completeness]
    if rows:
        con.executemany("insert into run_completeness values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("create schema main_dbt_test__audit")
    cols = {
        "adapter_ok_rate": "adapter varchar, roasters bigint, ok_share double",
        "run_ok_rate": "run_date date, roasters bigint, ok_share double",
        "suspect_runs_need_review": "roaster_id varchar, run_date date, batch_id varchar, products bigint, "
        "baseline_products double",
        "edge_challenge_share": "edge varchar, adapter varchar, roasters bigint, challenged bigint, "
        "challenged_share double",
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
    tests = {t: "pass" for t in fetchhealth.TESTS} | statuses
    return [
        {"results": [{"unique_id": "model.coffeedata.run_completeness", "status": "success"}]},
        {"results": [{"unique_id": f"test.coffeedata.{t}", "status": s} for t, s in tests.items()]},
    ]


def test_two_platforms_down_at_once_point_at_the_image_and_hold_every_repull(tmp_path):
    rc = [("a.com", "shopify", True, False, True, False), ("b.com", "woocommerce", True, False, True, False),
          ("c.com", "generic", True, False, True, False)]  # fmt: skip
    db = _db(tmp_path, rc, {"adapter_ok_rate": [("shopify", 40, 0.1), ("woocommerce", 30, 0.2)],
                            "run_ok_rate": [(date(2026, 11, 2), 70, 0.14)]})  # fmt: skip
    out = fetchhealth.judge(db, _results(adapter_ok_rate="fail", run_ok_rate="fail"))
    assert [s["scope"] for s in out["systemic"]] == ["connector", "connector", "image"]
    assert out["repull"] == [] and out["held_back"] == 3  # generic's too: the image is shared


def test_the_run_below_its_floor_with_no_platform_to_blame_is_systemic(tmp_path):
    db = _db(tmp_path, [("a.com", "generic", True, True, True, False)], {"run_ok_rate": [(date(2026, 11, 2), 30,
                                                                                          0.8)]})  # fmt: skip
    out = fetchhealth.judge(db, _results(run_ok_rate="fail"))
    assert out["systemic"] == [{"scope": "run", "rule": "run_ok_rate", "roasters": 30, "ok_share": 0.8}]
    assert out["repull"] == [
        {"roaster_id": "a.com", "adapter": "generic", "reasons": ["empty"], "rules": ["run_ok_rate"]}
    ]
    # While a connector still crawls, it is left out, and the run as a whole isn't judged yet.
    out = fetchhealth.judge(db, _results(run_ok_rate="fail"), running=frozenset({"generic"}))
    assert out["systemic"] == [] and out["repull"] == [] and out["running"] == ["generic"]


def test_a_dbt_error_is_no_verdict_not_a_failed_fetch(tmp_path):
    db = _db(tmp_path, [], {})
    with pytest.raises(fetchhealth.CheckError, match="run_completeness"):
        fetchhealth.judge(
            db, [{"results": [{"unique_id": "model.coffeedata.run_completeness", "status": "error"}]}, {}]
        )
    with pytest.raises(fetchhealth.CheckError):  # a renamed test that no longer runs
        fetchhealth.judge(db, _results()[:1] + [{"results": []}])
    with pytest.raises(
        fetchhealth.CheckError, match="adapter_ok_rate says fail but stored 0"
    ):  # never evaluated
        fetchhealth.judge(db, _results(adapter_ok_rate="fail"))


def test_a_poll_judges_only_when_a_crawl_attempt_has_ended_since_the_last_check(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    judged = []
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (True, []))
    monkeypatch.setattr(fetchhealth, "judge", lambda db, results, running=frozenset(): judged.append(running) or {
        "systemic": [], "repull": [], "held_back": 0, "by_connector": [], "running": sorted(running)})  # fmt: skip
    assert fetchhealth.run(lk, WEEK)["status"].startswith("waiting")  # no crawl yet
    shop = control.start(lk, "crawl", WEEK, "shopify")
    assert fetchhealth.run(lk, WEEK)["status"].startswith("waiting")  # running, nothing ended
    control.finish(lk, control.start(lk, "crawl", WEEK, "woocommerce"), "done")
    assert fetchhealth.run(lk, WEEK)["status"] == "checked" and judged == [frozenset({"shopify"})]
    assert fetchhealth.run(lk, WEEK)["status"].startswith("up to date")
    control.finish(lk, shop, "deadline")
    assert fetchhealth.run(lk, WEEK)["status"] == "checked" and judged[-1] == frozenset()
    # A confirmation arm's finished crawl is not a pass of the week: nothing new to judge.
    control.finish(lk, control.start(lk, "crawl", WEEK, "squarespace"), "done", detail={"slice": "0/2"})
    assert fetchhealth.run(lk, WEEK)["status"].startswith("up to date")


def test_a_check_that_could_not_run_alerts_once_not_on_every_poll(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (False, [{}, {}]))  # dbt crashed
    control.finish(lk, control.start(lk, "crawl", WEEK, "woocommerce"), "done")
    with pytest.raises(fetchhealth.CheckError):
        fetchhealth.run(lk, WEEK)
    assert [r["status"] for r in control.rows(lk, WEEK) if r["job"] == "fetch-health"] == ["failed"]
    assert fetchhealth.run(lk, WEEK)["status"].startswith("up to date")
    with pytest.raises(fetchhealth.CheckError):  # --force tries again
        fetchhealth.run(lk, WEEK, force=True)


def test_a_crawl_that_died_counts_as_ended(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (True, []))
    monkeypatch.setattr(fetchhealth, "judge", lambda db, results, running=frozenset(): {
        "systemic": [], "repull": [], "held_back": 0, "by_connector": [], "running": sorted(running)})  # fmt: skip
    # Only its running row, started longer ago than the jobs' time limit: killed or timed out.
    old = control.start(lk, "crawl", WEEK, "generic")
    control.write(lk, old | {"started_at": old["started_at"] - fetchhealth.JOB_LIMIT - timedelta(minutes=1)})
    out = fetchhealth.run(lk, WEEK)
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
        job = control.start(lk, fetchhealth.JOB, WEEK)
        control.finish(lk, job, "done", detail={"repull": repull, "running": list(running)})

    check([{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["suspect"]},
           {"roaster_id": "b.com", "adapter": "shopify", "reasons": ["empty"]},
           {"roaster_id": "c.com", "adapter": "shopify", "reasons": ["failed"]},  # every crawl pass asks it anyway
           {"roaster_id": "w.com", "adapter": "woocommerce", "reasons": ["suspect"]}])  # fmt: skip
    # A later check that ran while Shopify still crawled said nothing about it: for Shopify the earlier one holds;
    # for WooCommerce the later one does (it found the same store still empty).
    check([{"roaster_id": "w.com", "adapter": "woocommerce", "reasons": ["suspect"]}], running=["shopify"])
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    ids = lambda: sorted(s.roaster.roaster_id for s in fetchhealth.repull_states(part, "shopify", WEEK))  # noqa: E731
    assert ids() == ["a.com", "b.com"]
    # Fetched again since the check (the resume slot did it): not asked a third time.
    run(part, WEEK, "a.com", [product("x")], hour=23)
    assert ids() == ["b.com"]
    assert [s.roaster.roaster_id for s in fetchhealth.repull_states(lk, "woocommerce", WEEK)] == ["w.com"]


def test_crawl_retry_also_fetches_the_repull_list_but_not_the_failed(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv(fetchhealth.REFETCH_ENV, "1")
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com"), _state("f.com")])
    control.finish(lk, control.start(lk, fetchhealth.JOB, WEEK), "done", detail={"repull": [
        {"roaster_id": "a.com", "adapter": "shopify", "reasons": ["empty"]},
        {"roaster_id": "f.com", "adapter": "shopify", "reasons": ["failed"]}]})  # fmt: skip
    seen = {}
    monkeypatch.setattr(ingest, "run", lambda lake, states, *a, **k: seen.setdefault("ids", [
        s.roaster.roaster_id for s in states]) and {"by_adapter": {}})  # fmt: skip
    cli.main(["crawl-retry", "--connector", "shopify", "--run-date", WEEK])
    assert seen["ids"] == ["a.com"]


def _check(lk: Lake, repull: list[dict]) -> None:
    control.finish(lk, control.start(lk, fetchhealth.JOB, WEEK), "done", detail={"repull": repull})


def test_a_repull_read_that_fails_transiently_is_retried(tmp_path, monkeypatch):
    """The repull's lake reads go through retry_transient, as every first read of a crawl: one dropped connection
    (the NAT out of ports) is retried, not the end of the pass."""
    from coffeedata import lake as lake_mod

    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com")])
    _check(lk, [{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["empty"]}])
    waits: list = []
    monkeypatch.setattr(lake_mod.time, "sleep", waits.append)
    real, calls = control.rows, []

    def flaky(lake, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise duckdb.IOException("IO Error: Could not connect to server: HEAD https://huggingface.co/x")
        return real(lake, *a, **k)

    monkeypatch.setattr(control, "rows", flaky)
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    assert [s.roaster.roaster_id for s in fetchhealth.repull_states(part, "shopify", WEEK)] == ["a.com"]
    assert len(calls) == 2 and waits == [lake_mod.RETRY_WAITS[0]]


@pytest.mark.parametrize("command", ["crawl", "crawl-retry"])
def test_a_repull_that_cannot_be_read_skips_only_the_repull_never_the_pass(tmp_path, monkeypatch, command):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv(fetchhealth.REFETCH_ENV, "1")
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com"), _state("b.com")])
    _check(lk, [{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["empty"]}])
    monkeypatch.setattr(fetchhealth, "repull_states", lambda *a: (_ for _ in ()).throw(
        duckdb.IOException("IO Error: Could not connect to server")))  # fmt: skip
    seen: dict = {}
    monkeypatch.setattr(ingest, "run", lambda lake, states, *a, **k: seen.__setitem__("ids", sorted(
        s.roaster.roaster_id for s in states)) or {"roasters": 0, "by_adapter": {}, "skipped_deadline": 0})  # fmt: skip
    cli.main([command, "--connector", "shopify", "--run-date", WEEK])
    # The pass ran its own work (crawl: every roaster without an ok fetch; crawl-retry: no parked store) and ended.
    assert seen.get("ids", []) == (["a.com", "b.com"] if command == "crawl" else [])
    assert [r["status"] for r in control.rows(lk, WEEK) if r["job"] == command] == ["done"]


def test_an_unreadable_check_detail_lists_no_repull(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com")])
    job = control.start(lk, fetchhealth.JOB, WEEK)
    control.write(lk, job | {"status": "done", "ended_at": job["started_at"], "detail": "{not json"})
    assert fetchhealth.repull_states(connectors.PartitionLake(lk, "shopify", WEEK), "shopify", WEEK) == []


def test_a_roaster_select_states_would_not_crawl_is_never_repulled(tmp_path, monkeypatch):
    """Opted out (or no platform any more) after the check: select_states' rules apply to the repull too."""
    lk = Lake(str(tmp_path / "lake"))
    gone = registry.RoasterState(Roaster(roaster_id="o.com", name="o", host="o.com", platform="shopify"),
                                 "opted_out", None)  # fmt: skip
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [_state("a.com"), gone, _state("n.com", "")]
    )
    _check(
        lk,
        [
            {"roaster_id": r, "adapter": "shopify", "reasons": ["suspect"]}
            for r in ("a.com", "n.com", "o.com")
        ],
    )
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    assert [s.roaster.roaster_id for s in fetchhealth.repull_states(part, "shopify", WEEK)] == ["a.com"]


def test_a_repull_is_fetched_without_a_known_product_count(tmp_path, monkeypatch):
    """The suspect fetch's short count is its latest complete ok fetch: crawl and crawl-retry pass the repulls as
    `refetch`, so ingest.run reads their whole catalog."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv(fetchhealth.REFETCH_ENV, "1")
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com")])
    monkeypatch.setattr(
        control, "now", lambda: datetime(2026, 11, 2, 18, tzinfo=UTC)
    )  # the check: after the fetch
    run(connectors.PartitionLake(lk, "shopify", WEEK), WEEK, "a.com", [product("x")], hour=1)
    _check(lk, [{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["suspect"]}])
    seen = []
    monkeypatch.setattr(ingest, "run", lambda lake, states, *a, **k: seen.append(k.get("refetch")) or {
        "roasters": 0, "by_adapter": {}, "skipped_deadline": 0})  # fmt: skip
    cli.main(["crawl", "--connector", "shopify", "--run-date", WEEK])
    cli.main(["crawl-retry", "--connector", "shopify", "--run-date", WEEK])
    assert seen == [{"a.com"}, {"a.com"}]


def test_a_suspect_with_no_adapter_is_listed_apart_never_repulled_or_held_back(tmp_path):
    rc = [("a.com", "shopify", True, False, True, False)]
    suspect = [("z.com", date(2026, 11, 2), "b1", 5, 30.0)]
    db = _db(tmp_path, rc, {"suspect_runs_need_review": suspect})
    out = fetchhealth.judge(db, _results(suspect_runs_need_review="warn"))
    assert [e["roaster_id"] for e in out["repull"]] == ["a.com"] and out["held_back"] == 0
    assert out["unattributed"] == [{"roaster_id": "z.com", "adapter": None, "reasons": ["suspect"],
                                    "rules": ["suspect_runs_need_review"], "products": 5,
                                    "baseline_products": 30.0}]  # fmt: skip
    # The same when everything is held back: it is not counted as held.
    (tmp_path / "dq.duckdb").unlink()
    db = _db(tmp_path, rc, {"suspect_runs_need_review": suspect,
                            "adapter_ok_rate": [("shopify", 40, 0.1), ("woocommerce", 30, 0.2)]})  # fmt: skip
    out = fetchhealth.judge(db, _results(suspect_runs_need_review="warn", adapter_ok_rate="fail"))
    assert (
        out["repull"] == []
        and out["held_back"] == 1
        and [e["roaster_id"] for e in out["unattributed"]] == ["z.com"]
    )


def test_one_systemic_finding_alerts_once_a_week_and_a_different_one_alerts_again(
    tmp_path, monkeypatch, capsys
):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (True, []))
    shop = {
        "scope": "connector",
        "connector": "shopify",
        "rule": "adapter_ok_rate",
        "roasters": 30,
        "ok_share": 0.1,
    }
    found: list = [[shop]]
    monkeypatch.setattr(fetchhealth, "judge", lambda db, results, running=frozenset(): {
        "systemic": found[-1], "repull": [], "held_back": 0, "by_connector": [], "running": []})  # fmt: skip

    def poll(connector: str) -> bool:
        control.finish(lk, control.start(lk, "crawl", WEEK, connector), "done")
        try:
            cli.main(["fetch-health", "--run-date", WEEK])
        except SystemExit:
            return True
        return False

    assert poll("shopify")  # the first finding emails
    assert not poll("woocommerce")  # found again after another pass: logged, not emailed
    assert '"severity": "WARNING"' in capsys.readouterr().out
    assert not poll("shopify")  # Shopify's resume, same outage, ok_share moved: still the same finding
    found.append([shop | {"ok_share": 0.2}])
    assert not poll("generic")
    woo = shop | {"connector": "woocommerce"}
    found.append([shop, woo, {"scope": "image", "connectors": ["shopify", "woocommerce"], "rule": "x"}])
    assert poll("square_online")  # a different finding: emails
    assert (
        fetchhealth.run(lk, WEEK, force=True)["alert"] == []
    )  # every finding of it already alerted this week


def test_every_repull_reason_is_a_named_gate_rule():
    """Every reason on the list is what a named rule says, a dbt test the gate runs, never an ad hoc check."""
    from pathlib import Path

    assert set(fetchhealth.RULES.values()) <= set(fetchhealth.GATE_TESTS)
    for rule in fetchhealth.RULES.values():
        assert (Path(__file__).parents[1] / "dbt" / "tests" / f"{rule}.sql").exists()


def test_a_repull_entry_with_no_named_rule_is_never_fetched(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com"), _state("b.com")])
    _check(lk, [{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["looks_odd"]},
                {"roaster_id": "b.com", "adapter": "shopify", "reasons": ["empty"]}])  # fmt: skip
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    assert [s.roaster.roaster_id for s in fetchhealth.repull_states(part, "shopify", WEEK)] == ["b.com"]


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
@pytest.mark.parametrize("command", ["crawl", "crawl-retry"])
def test_with_refetch_off_crawl_and_crawl_retry_ignore_the_repull_list(tmp_path, monkeypatch, command, value):
    """Re-fetching ships switched off: with the setting unset (the default) or off, neither pass reads the list, and each
    fetches exactly what it would on main, with no `refetch` (known product counts kept)."""
    if value is None:
        monkeypatch.delenv(fetchhealth.REFETCH_ENV, raising=False)
    else:
        monkeypatch.setenv(fetchhealth.REFETCH_ENV, value)
    assert not fetchhealth.refetch_enabled()
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(control, "now", lambda: datetime(2026, 11, 2, 18, tzinfo=UTC))
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state("a.com"), _state("b.com")])
    # a.com fetched ok (then listed as suspect), b.com never fetched: main's crawl asks only b.com, crawl-retry nobody.
    run(connectors.PartitionLake(lk, "shopify", WEEK), WEEK, "a.com", [product("x")], hour=1)
    _check(lk, [{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["suspect"],
                 "rules": ["suspect_runs_need_review"]}])  # fmt: skip
    read: list = []
    real = fetchhealth.repull_states
    monkeypatch.setattr(fetchhealth, "repull_states", lambda *a: read.append(a) or real(*a))
    seen: list = []
    monkeypatch.setattr(ingest, "run", lambda lake, states, *a, **k: seen.append((sorted(
        s.roaster.roaster_id for s in states), k.get("refetch"))) or {"roasters": 0, "by_adapter": {},
                                                                     "skipped_deadline": 0})  # fmt: skip
    cli.main([command, "--connector", "shopify", "--run-date", WEEK])
    assert read == []  # the list is never read
    if command == "crawl":  # main's work: the roaster without an ok fetch, never a.com
        assert seen == [(["b.com"], set())]
    else:  # main's work: no parked store, so nothing (whether or not ingest.run is called with no roasters)
        assert all(ids == [] and not refetch for ids, refetch in seen)
    # The list is still there for the owner.
    dq = [r for r in control.rows(lk, WEEK) if r["job"] == "fetch-health"]
    assert json.loads(dq[-1]["detail"])["repull"][0]["roaster_id"] == "a.com"
    # And turned on, the same pass re-fetches it (the contrast that makes the test mean something).
    monkeypatch.setenv(fetchhealth.REFETCH_ENV, "1")
    seen.clear()
    cli.main([command, "--connector", "shopify", "--run-date", WEEK])
    assert read and seen and "a.com" in seen[-1][0] and seen[-1][1] == {"a.com"}


def test_mains_fetch_states_are_each_mapped_on_purpose(tmp_path, monkeypatch):
    """Over a real dbt build: a partial fetch is not a failure (listed apart, never re-fetched or held); a
    quarantined store is left out of the ok rates and listed apart; a challenged store is a failure kept on the list
    with its edge named; a bare 202 names no edge and is a plain failure."""
    lk = _week(tmp_path, monkeypatch)
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    run(
        part,
        WEEK,
        "s30.com",
        [product("s30.com-0")],
        partial=True,
        error="partial: deadline reached (page 2)",
    )
    run(part, WEEK, "s31.com", None, status="crashed")
    run(
        part,
        WEEK,
        "s32.com",
        None,
        error="FetchError: HTTP 202 (a challenge) https://s32.com",
        blocks=[_sg("s32")],
    )
    run(part, WEEK, "s33.com", None, error="FetchError: HTTP 202 https://s33.com/products.json")
    out = fetchhealth.run(lk, WEEK)
    assert out["partial"] == [
        {"roaster_id": "s30.com", "adapter": "shopify", "reason": "partial: deadline reached (page 2)"}
    ]
    assert out["quarantined"] == [{"roaster_id": "s31.com", "adapter": "shopify"}]
    # One SiteGround store among 33 is no systemic challenge (edge_challenge_share). 29 of 33 ok is under
    # run_ok_rate's 90% with no platform below its floor: the run is a finding of its own.
    assert [f["scope"] for f in out["systemic"]] == ["run"]
    assert out["tests"]["edge_challenge_share"] == "pass"
    assert [(e["roaster_id"], e["reasons"], e.get("challenge")) for e in out["repull"]] == [
        ("s00.com", ["suspect"], None), ("s01.com", ["failed"], None), ("s02.com", ["empty"], None),
        ("s32.com", ["failed"], "siteground"), ("s33.com", ["failed"], None),
    ]  # fmt: skip
    assert out["held_back"] == 0
    shop = next(c for c in out["by_connector"] if c["adapter"] == "shopify")
    assert (shop["partial"], shop["quarantined"], shop["challenged"]) == (1, 1, 1)
    # The quarantined store is not in the ok rates' population; the partial one counts as ok (it has products).
    assert (shop["attempted"], shop["judged"], shop["ok"]) == (34, 33, 29)


def _sg(host: str) -> dict:
    """A recorded block with SiteGround's challenge header."""
    return {"status": 202, "host": f"{host}.com", "path": "/products.json", "sg_captcha": "challenge"}


def _poll(lk: Lake, connector: str = "shopify") -> bool:
    """A crawl attempt ends and the scheduled poll runs: whether it alerted (exited non-zero)."""
    control.finish(lk, control.start(lk, "crawl", WEEK, connector), "done")
    try:
        cli.main(["fetch-health", "--run-date", WEEK])
    except SystemExit:
        return True
    return False


def test_one_challenged_store_alerts_nothing(tmp_path, monkeypatch, capsys):
    lk = _week(tmp_path, monkeypatch)
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    run(
        part,
        WEEK,
        "s30.com",
        None,
        error="FetchError: HTTP 202 (a challenge) https://s30.com",
        blocks=[_sg("s30")],
    )
    assert not _poll(lk)  # 28 of 31 ok passes run_ok_rate; 1 of 31 challenged is under edge_challenge_share
    printed = capsys.readouterr().out
    assert '"severity"' not in printed  # no ERROR line, no WARNING line: nothing systemic
    detail = json.loads([r for r in control.rows(lk, WEEK) if r["job"] == fetchhealth.JOB][-1]["detail"])
    assert detail["systemic"] == [] and detail["alert"] == []
    assert {"roaster_id": "s30.com", "adapter": "shopify", "reasons": ["failed"], "rules": ["run_ok_rate"],
            "challenge": "siteground"} in detail["repull"]  # fmt: skip


def test_a_systemic_challenge_alerts_once(tmp_path, monkeypatch, capsys):
    lk = _week(tmp_path, monkeypatch)
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    for rid in SHOPS[30:35]:  # 5 of the 35 reached: 14%, over the run's 10% tolerance, on a platform of 20+
        run(
            part,
            WEEK,
            rid,
            None,
            error=f"FetchError: HTTP 202 (a challenge) https://{rid}",
            blocks=[_sg(rid[:-4])],
        )
    assert _poll(lk)
    alert = json.loads(next(x for x in capsys.readouterr().out.splitlines() if '"severity": "ERROR"' in x))
    assert {"scope": "challenge", "edge": "siteground", "connectors": ["shopify"], "rule": "edge_challenge_share",
            "roasters": 5} in alert["systemic"]  # fmt: skip
    detail = json.loads([r for r in control.rows(lk, WEEK) if r["job"] == fetchhealth.JOB][-1]["detail"])
    # Its roasters stay on the list, each with the edge named.
    assert (
        sorted(e["roaster_id"] for e in detail["repull"] if e.get("challenge") == "siteground")
        == SHOPS[30:35]
    )
    assert not _poll(lk)  # the next pass's check finds it again: logged, not emailed
    assert '"severity": "WARNING"' in capsys.readouterr().out


def test_the_2026_11_02_shape_bare_202s_and_a_few_cloudflare_stores_alert_nothing(
    tmp_path, monkeypatch, capsys
):
    """2026-11-02's replay: no gate test failed, but 27 WooCommerce stores (of 229) failed with a bare 202 and 5
    generic ones (of 167) behind Cloudflare. A bare 202 names no edge, so however many there are it is no
    challenge finding (abstain), only failed fetches; a few Cloudflare stores are under edge_challenge_share."""
    lk = _week(tmp_path, monkeypatch)
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    for rid in SHOPS[30:34]:  # 4 of 34: 12%, which would be over the share if a bare 202 were an edge
        run(part, WEEK, rid, None, error=f"FetchError: HTTP 202 https://{rid}/products.json",
            blocks=[{"status": 202, "host": rid, "path": "/products.json"}])  # fmt: skip
    run(part, WEEK, "s34.com", None, error="FetchError: HTTP 403 https://s34.com",
        blocks=[{"status": 403, "host": "s34.com", "path": "/", "cf_mitigated": "challenge"}])  # fmt: skip
    out = fetchhealth.run(lk, WEEK)
    assert [f for f in out["systemic"] if f["scope"] == "challenge"] == []
    assert out["tests"]["edge_challenge_share"] == "pass"
    entries = {e["roaster_id"]: e for e in out["repull"]}
    assert all(entries[r]["reasons"] == ["failed"] and "challenge" not in entries[r] for r in SHOPS[30:34])
    assert entries["s34.com"]["challenge"] == "cloudflare" and out["held_back"] == 0


def test_a_partial_or_quarantined_store_is_never_a_reason_and_a_challenge_needs_its_share(tmp_path):
    rc = [("p.com", "woocommerce", True, True, True, True, False, True, "partial: 40 of 90+ products read", None),
          # a partial fetch that read nothing (a page block before page 1): partial, never `empty`
          ("p0.com", "woocommerce", True, True, True, False, False, True, "partial: page 1 blocked", None),
          ("q.com", "generic", True, False, False, False, True, False, None, None),
          ("sg1.com", "woocommerce", True, False, True, False, False, False, None, "siteground"),
          ("sg2.com", "generic", True, False, True, False, False, False, None, "siteground"),
          ("f.com", "woocommerce", True, False, True, False)]  # fmt: skip
    out = fetchhealth.judge(_db(tmp_path, rc, {}), _results())
    assert out["systemic"] == []  # edge_challenge_share passed: the challenges are per-roaster failures
    assert [(e["roaster_id"], e["reasons"], e.get("challenge")) for e in out["repull"]] == [
        ("sg2.com", ["failed"], "siteground"), ("f.com", ["failed"], None), ("sg1.com", ["failed"], "siteground"),
    ]  # fmt: skip
    assert out["held_back"] == 0
    assert [r["roaster_id"] for r in out["partial"]] == ["p.com", "p0.com"]
    assert [r["roaster_id"] for r in out["quarantined"]] == ["q.com"]
    # Past the share, one finding per edge, naming each platform the test failed for; the roasters stay listed.
    (tmp_path / "w").mkdir()
    share = {"edge_challenge_share": [("siteground", "woocommerce", 40, 6, 0.15), ("siteground", "generic", 30, 4,
                                                                                    0.133)]}  # fmt: skip
    out = fetchhealth.judge(_db(tmp_path / "w", rc, share), _results(edge_challenge_share="warn"))
    assert out["systemic"] == [{"scope": "challenge", "edge": "siteground", "connectors": ["generic", "woocommerce"],
                                "rule": "edge_challenge_share", "roasters": 10}]  # fmt: skip
    assert len(out["repull"]) == 3 and out["held_back"] == 0
    # While woocommerce still crawls, its part of the finding and its partial stores wait for its pass to end.
    (tmp_path / "r").mkdir()
    out = fetchhealth.judge(_db(tmp_path / "r", rc, share), _results(edge_challenge_share="warn"),
                            running=frozenset({"woocommerce"}))  # fmt: skip
    assert [f["connectors"] for f in out["systemic"]] == [["generic"]] and out["partial"] == []


def test_a_challenge_found_again_on_more_platforms_is_the_same_finding():
    one = {"scope": "challenge", "edge": "siteground", "connectors": ["woocommerce"], "roasters": 3}
    more = one | {"connectors": ["generic", "woocommerce"], "roasters": 9}
    assert fetchhealth.finding_key(one) == fetchhealth.finding_key(more) == ("challenge", ("siteground",))
    assert fetchhealth.finding_key(one) != fetchhealth.finding_key(one | {"edge": "cloudflare"})
