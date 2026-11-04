"""fetch-health: the gate's own fetch rules on the week's unmerged crawl output, its list, and systemic alerts. It
judges whether each fetch returned a catalog of about the right size, never whether the data is good."""

import json
import shutil
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
    tests = {t: "pass" for t in fetchhealth.GATE_TESTS} | statuses
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
    assert (
        out["systemic"][-1]["rule"] == "adapter_ok_rate"
    )  # the image finding is adapter_ok_rate's, on 2+ platforms
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stub_judge(monkeypatch, seen: list | None = None) -> None:
    monkeypatch.setattr(cli, "dbt_build", lambda *a, **k: (True, []))
    monkeypatch.setattr(fetchhealth, "judge", lambda db, results, running=frozenset(): (
        seen.append(running) if seen is not None else None) or {"systemic": [], "repull": [], "held_back": 0,
                                                                 "by_connector": [], "running": sorted(running)})  # fmt: skip


def test_a_dead_attempt_is_judged_once_a_later_attempt_of_its_job_and_connector_has_started(
    tmp_path, monkeypatch
):
    """2026-11-02's generic OOM: the killed attempt keeps only its running row, and Cloud Run's retry starts at once.
    The retry shows the first is dead, so its connector is judged now, not 240 minutes after it started."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    seen: list = []
    _stub_judge(monkeypatch, seen)
    clock = iter(datetime(2026, 11, 2, 9, 17, tzinfo=UTC) + timedelta(minutes=i) for i in range(100))
    monkeypatch.setattr(control, "now", lambda: next(clock))
    control.start(lk, "crawl", WEEK, "generic")  # killed at 09:24: only its running row
    assert fetchhealth.run(lk, WEEK)["status"].startswith(
        "waiting"
    )  # nothing ended yet: it may still be running
    retry = control.start(lk, "crawl", WEEK, "generic")  # the retry starts
    out = fetchhealth.run(lk, WEEK)
    assert out["status"] == "checked" and out["died"] == ["generic"]
    assert seen[-1] == frozenset({"generic"})  # the retry itself is still running, so generic waits for it
    # Another connector's still-running attempt is no later attempt of generic's: it doesn't kill anything.
    control.start(lk, "crawl", WEEK, "shopify")
    assert fetchhealth.run(lk, WEEK)["status"].startswith("up to date")
    control.finish(lk, retry, "done")
    out = fetchhealth.run(lk, WEEK)
    assert out["status"] == "checked" and seen[-1] == frozenset({"shopify"})


def test_a_poll_while_another_check_runs_is_up_to_date(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    seen: list = []
    _stub_judge(monkeypatch, seen)
    control.finish(lk, control.start(lk, "crawl", WEEK, "woocommerce"), "done")
    # A check whose running row is older than the check's own limit died: it doesn't hold the next poll back.
    stale = _utc_now() - fetchhealth.CHECK_LIMIT - timedelta(minutes=1)
    control.write(lk, {"run_date": date.fromisoformat(WEEK), "job": fetchhealth.JOB, "attempt_id": "stale-check",
                       "started_at": stale, "status": "running"})  # fmt: skip
    assert fetchhealth.run(lk, WEEK)["status"] == "checked" and len(seen) == 1
    # One still running (its running row younger than the limit) does: two checks at once could alert twice.
    control.finish(lk, control.start(lk, "crawl", WEEK, "generic"), "done")
    control.start(lk, fetchhealth.JOB, WEEK)
    out = fetchhealth.run(lk, WEEK)
    assert out["status"] == "up to date: another check is still running" and len(seen) == 1
    assert fetchhealth.run(lk, WEEK, force=True)["status"] == "checked"  # --force runs anyway


def _row(lk: Lake, job: str, started: datetime, status: str = "running", connector: str | None = None,
         detail: dict | None = None, attempt: str | None = None) -> None:  # fmt: skip
    """A job_manifests row written as it stands: e.g. a killed attempt's lone running row."""
    control.write(lk, {"run_date": date.fromisoformat(WEEK), "job": job, "connector": connector, "detail": detail,
                       "attempt_id": attempt or f"{job}-{connector}-{started:%H%M%S}", "started_at": started,
                       "ended_at": None if status == "running" else started + timedelta(minutes=5),
                       "status": status})  # fmt: skip


def test_a_killed_check_holds_back_at_most_its_own_limit_not_the_crawls(tmp_path, monkeypatch):
    """The review's case: a check killed at 23:07 leaves only its running row. The 21:47 pass ends at 01:40, and the
    01:47 poll judges it: the killed check is dead 30 minutes after it started, not 240."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    seen: list = []
    _stub_judge(monkeypatch, seen)
    day = datetime(2026, 11, 2, tzinfo=UTC)
    _row(lk, fetchhealth.JOB, day + timedelta(hours=23, minutes=7))  # killed: only its running row
    clock = [day + timedelta(hours=23, minutes=27)]
    monkeypatch.setattr(control, "now", lambda: clock[0])
    _row(lk, "crawl", day + timedelta(hours=21, minutes=47), "done", "generic")
    # 23:27: the killed check started 20 minutes ago, inside a check's limit: it may still be running.
    assert fetchhealth.run(lk, WEEK)["status"] == "up to date: another check is still running" and seen == []
    clock[0] = day + timedelta(hours=25, minutes=47)  # Tuesday 01:47
    assert fetchhealth.run(lk, WEEK)["status"] == "checked" and len(seen) == 1
    assert fetchhealth.CHECK_LIMIT < fetchhealth.JOB_LIMIT


def test_a_check_with_only_its_running_row_is_dead_once_a_later_check_has_started(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    seen: list = []
    _stub_judge(monkeypatch, seen)
    now = _utc_now()
    _row(lk, fetchhealth.JOB, now - timedelta(minutes=10), attempt="killed")  # young, but killed
    _row(
        lk, fetchhealth.JOB, now - timedelta(minutes=5), "done", attempt="later"
    )  # a later check ran after it
    _row(lk, "crawl", now - timedelta(minutes=2), "done", "generic")
    assert fetchhealth.run(lk, WEEK)["status"] == "checked" and len(seen) == 1


def test_a_manual_only_crawl_never_marks_the_pass_beside_it_dead(tmp_path, monkeypatch):
    """RUNBOOK §8's `crawl --connector X --only <roaster>` during X's pass is no retry of it: X stays running (left
    out until its pass ends), and nothing is reported dead. A real later attempt of the pass still is."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    seen: list = []
    _stub_judge(monkeypatch, seen)
    now = _utc_now()
    _row(
        lk, "crawl", now - timedelta(minutes=60), connector="woocommerce", attempt="pass"
    )  # the resume, running
    _row(
        lk,
        "crawl",
        now - timedelta(minutes=20),
        "done",
        "woocommerce",
        detail={"only": "a.com"},
        attempt="only",
    )
    out = fetchhealth.run(lk, WEEK)
    assert out["status"] == "checked" and out["died"] == [] and seen[-1] == frozenset({"woocommerce"})
    _row(
        lk, "crawl", now - timedelta(minutes=10), connector="woocommerce", attempt="retry"
    )  # Cloud Run's retry
    out = fetchhealth.run(lk, WEEK)
    assert out["died"] == ["woocommerce"] and seen[-1] == frozenset({"woocommerce"})  # the retry still runs


def test_the_polls_first_lake_read_is_retried_on_a_transient_error(tmp_path, monkeypatch):
    """One dropped connection on the poll's first read is retried, not an execution that fails and emails; one
    that keeps failing still fails the poll."""
    from coffeedata import lake as lake_mod

    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    waits: list = []
    monkeypatch.setattr(lake_mod.time, "sleep", waits.append)
    real, calls = control.rows, []

    def flaky(lake, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise duckdb.IOException("IO Error: HTTP 503: connection reset by peer")
        return real(lake, *a, **k)

    monkeypatch.setattr(control, "rows", flaky)
    assert fetchhealth.run(lk, WEEK)["status"].startswith("waiting")  # an empty week, read on the second try
    assert len(calls) == 2 and waits == [lake_mod.RETRY_WAITS[0]]

    def down(lake, *a, **k):
        raise duckdb.IOException("IO Error: Could not connect to server: HEAD https://huggingface.co/x")

    monkeypatch.setattr(control, "rows", down)
    waits.clear()
    with pytest.raises(duckdb.IOException):
        fetchhealth.run(lk, WEEK)
    assert waits == list(lake_mod.RETRY_WAITS)  # every retry, then the error


def test_the_verdict_is_published_and_read_from_another_staging_root(tmp_path, monkeypatch, caplog):
    """R2: two instances, each with its own staging folder over one dataset. The check's verdict is published right
    after its done row, so another instance's crawl pass reads it. A publish that fails changes nothing else."""
    url = "hf://datasets/example/coffee"
    monkeypatch.setattr(Lake, "remote_has", lambda self, table: False)  # read local files only: no network
    a = Lake(url, staging=tmp_path / "stage-a")  # the fetch-health job's instance
    b = Lake(url, staging=tmp_path / "stage-b")  # a crawl pass's instance

    def publish(self, message, *args, **kwargs):  # the dataset is stage-b's copy: publish copies across
        for f in sorted(self.root.rglob("*.parquet")):
            dest = tmp_path / "stage-b" / f.relative_to(self.root)
            if self.root == tmp_path / "stage-a" and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
        return 1

    monkeypatch.setattr(Lake, "publish", publish)
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    monkeypatch.setattr(registry, "current", lambda lake, *x, **k: [_state("a.com")])
    monkeypatch.setattr(cli, "dbt_build", lambda *x, **k: (True, []))
    listed = [{"roaster_id": "a.com", "adapter": "shopify", "reasons": ["empty"], "rules": ["run_ok_rate"]}]
    monkeypatch.setattr(fetchhealth, "judge", lambda db, results, running=frozenset(): {
        "systemic": [], "repull": listed, "held_back": 0, "by_connector": [], "running": []})  # fmt: skip
    control.finish(a, control.start(a, "crawl", WEEK, "shopify"), "done")
    assert fetchhealth.run(a, WEEK)["status"] == "checked"
    seen = [r for r in control.rows(b, WEEK) if r["job"] == fetchhealth.JOB]
    assert [r["status"] for r in seen] == ["done"] and control.detail_of(seen[0])["repull"] == listed
    monkeypatch.setenv(fetchhealth.REFETCH_ENV, "1")
    part = connectors.PartitionLake(b, "shopify", WEEK)
    assert [s.roaster.roaster_id for s in fetchhealth.repull_states(part, "shopify", WEEK)] == ["a.com"]

    # A publish that fails: a WARNING, and the same verdict and status (so the same exit code).
    def broken(self, *args, **kwargs):
        raise OSError("dataset unreachable")

    monkeypatch.setattr(Lake, "publish", broken)
    control.finish(a, control.start(a, "crawl", WEEK, "generic"), "done")
    with caplog.at_level("WARNING"):
        out = fetchhealth.run(a, WEEK)
    assert out["status"] == "checked" and out["repull"] == listed and out["alert"] == []
    assert "verdict wasn't published: OSError: dataset unreachable" in caplog.text


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
    """Over a real dbt build: a partial fetch is not a failure (listed apart, never re-fetched or held), with 0
    products too; a quarantined store is left out of the ok rates and listed apart; a challenged store is an ordinary
    failed fetch, listed with its edge named; a bare 202 names no edge."""
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
    run(part, WEEK, "s34.com", [], partial=True, error="partial: page 1 blocked")  # ok, partial, 0 products
    out = fetchhealth.run(lk, WEEK)
    assert out["partial"] == [
        {"roaster_id": "s30.com", "adapter": "shopify", "reason": "partial: deadline reached (page 2)"},
        {"roaster_id": "s34.com", "adapter": "shopify", "reason": "partial: page 1 blocked"},
    ]
    assert out["quarantined"] == [{"roaster_id": "s31.com", "adapter": "shopify"}]
    # 29 of 34 ok is under run_ok_rate's 90% with no platform below its floor: the run is a finding. No challenge
    # scope exists: a challenge is an ordinary failure.
    assert [f["scope"] for f in out["systemic"]] == ["run"]
    assert [(e["roaster_id"], e["reasons"], e.get("challenge")) for e in out["repull"]] == [
        ("s00.com", ["suspect"], None), ("s01.com", ["failed"], None), ("s02.com", ["empty"], None),
        ("s32.com", ["failed"], "siteground"), ("s33.com", ["failed"], None),
    ]  # fmt: skip
    assert out["held_back"] == 0
    shop = next(c for c in out["by_connector"] if c["adapter"] == "shopify")
    assert (shop["partial"], shop["quarantined"], shop["challenged"]) == (2, 1, 1)
    # The quarantined store is not in the ok rates' population; the partial one with products counts as ok.
    assert (shop["attempted"], shop["judged"], shop["ok"]) == (35, 34, 29)
    # The list's failed and empty rows are exactly run_ok_rate's not-ok rows, less the partial fetches.
    con = duckdb.connect(str(tmp_path / "fh" / "dq.duckdb"), read_only=True)
    not_ok = {r[0] for r in con.execute(
        "select roaster_id from run_completeness where has_catalog and attempted and not catalog_ok").fetchall()}  # fmt: skip
    listed = {e["roaster_id"] for e in out["repull"] if {"failed", "empty"} & set(e["reasons"])}
    partial0 = {r["roaster_id"] for r in out["partial"]} & not_ok
    assert partial0 == {"s34.com"} and listed | partial0 == not_ok and not listed & partial0


def test_last_weeks_suspect_is_never_listed_for_a_week_with_no_ok_fetch_yet(tmp_path, monkeypatch):
    """suspect_runs_need_review judges the checked week (as_of_run), not the latest week with an ok fetch: early in
    an outage, last week's unreviewed suspect would otherwise be listed as this week's."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setattr(fetchhealth, "DIR", tmp_path / "fh")
    monkeypatch.setattr(registry, "current", lambda lake, *a, **k: [_state(r) for r in SHOPS[:3]])
    for d in HISTORY[:3]:
        for rid in SHOPS[:3]:
            run(lk, d, rid, [product(f"{rid}-{n}") for n in range(30)])
    for rid in SHOPS[:3]:  # last week (10-26): s00 came back short, a suspect nobody reviewed
        run(lk, HISTORY[3], rid, [product(f"{rid}-{n}") for n in range(5 if rid == "s00.com" else 30)])
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    run(part, WEEK, "s00.com", None)  # this week: one fetch so far, and it failed
    control.finish(lk, control.start(lk, "crawl", WEEK, "shopify"), "deadline", skipped=2)
    out = fetchhealth.run(lk, WEEK)
    assert out["tests"]["suspect_runs_need_review"] == "pass"
    assert [(e["roaster_id"], e["reasons"]) for e in out["repull"]] == [("s00.com", ["failed"])]


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
    assert not _poll(lk)  # 28 of 31 ok passes run_ok_rate, and a challenge has no scope of its own
    printed = capsys.readouterr().out
    assert '"severity"' not in printed  # no ERROR line, no WARNING line: nothing systemic
    detail = json.loads([r for r in control.rows(lk, WEEK) if r["job"] == fetchhealth.JOB][-1]["detail"])
    assert detail["systemic"] == [] and detail["alert"] == []
    assert {"roaster_id": "s30.com", "adapter": "shopify", "reasons": ["failed"], "rules": ["run_ok_rate"],
            "challenge": "siteground"} in detail["repull"]  # fmt: skip


def test_many_challenged_stores_are_still_ordinary_failed_fetches(tmp_path, monkeypatch):
    """The owner's decision (2026-11-04): a challenge has no systemic scope. 5 SiteGround stores of 35 reached are 5
    `failed` entries naming their edge, never held back; only the gate's own floors decide what is systemic."""
    lk = _week(tmp_path, monkeypatch)
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    for rid in SHOPS[30:35]:
        run(
            part,
            WEEK,
            rid,
            None,
            error=f"FetchError: HTTP 202 (a challenge) https://{rid}",
            blocks=[_sg(rid[:-4])],
        )
    out = fetchhealth.run(lk, WEEK)
    assert [f["scope"] for f in out["systemic"]] == [
        "run"
    ]  # 28 of 35 ok: run_ok_rate's floor, not a challenge
    challenged = [e for e in out["repull"] if e.get("challenge")]
    assert [e["roaster_id"] for e in challenged] == SHOPS[30:35]
    assert all(e["reasons"] == ["failed"] and e["challenge"] == "siteground" for e in challenged)
    assert out["held_back"] == 0


def test_the_2026_11_02_shape_bare_202s_and_a_few_cloudflare_stores_alert_nothing(
    tmp_path, monkeypatch, capsys
):
    """2026-11-02's replay: no gate test failed, but 27 WooCommerce stores (of 229) failed with a bare 202 and 5
    generic ones (of 167) behind Cloudflare. A bare 202 names no edge (abstain): a plain failed fetch. A Cloudflare
    store is a failed fetch naming its edge. Neither alerts on its own."""
    lk = _week(tmp_path, monkeypatch)
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    for rid in SHOPS[30:34]:
        run(part, WEEK, rid, None, error=f"FetchError: HTTP 202 https://{rid}/products.json",
            blocks=[{"status": 202, "host": rid, "path": "/products.json"}])  # fmt: skip
    run(part, WEEK, "s34.com", None, error="FetchError: HTTP 403 https://s34.com",
        blocks=[{"status": 403, "host": "s34.com", "path": "/", "cf_mitigated": "challenge"}])  # fmt: skip
    out = fetchhealth.run(lk, WEEK)
    assert [f["scope"] for f in out["systemic"]] == ["run"]  # 28 of 35 ok: the gate's floor, nothing else
    entries = {e["roaster_id"]: e for e in out["repull"]}
    assert all(entries[r]["reasons"] == ["failed"] and "challenge" not in entries[r] for r in SHOPS[30:34])
    assert entries["s34.com"]["challenge"] == "cloudflare" and out["held_back"] == 0


def test_a_partial_or_quarantined_store_is_never_a_reason_and_a_challenge_is_a_plain_failure(tmp_path):
    rc = [("p.com", "woocommerce", True, True, True, True, False, True, "partial: 40 of 90+ products read", None),
          # a partial fetch that read nothing (a page block before page 1): partial, never `empty`
          ("p0.com", "woocommerce", True, True, True, False, False, True, "partial: page 1 blocked", None),
          ("q.com", "generic", True, False, False, False, True, False, None, None),
          ("sg1.com", "woocommerce", True, False, True, False, False, False, None, "siteground"),
          ("sg2.com", "generic", True, False, True, False, False, False, None, "siteground"),
          ("f.com", "woocommerce", True, False, True, False)]  # fmt: skip
    out = fetchhealth.judge(_db(tmp_path, rc, {}), _results())
    assert out["systemic"] == []  # challenges are per-roaster failures: no scope of their own
    assert [(e["roaster_id"], e["reasons"], e.get("challenge")) for e in out["repull"]] == [
        ("sg2.com", ["failed"], "siteground"), ("f.com", ["failed"], None), ("sg1.com", ["failed"], "siteground"),
    ]  # fmt: skip
    assert out["held_back"] == 0
    assert [r["roaster_id"] for r in out["partial"]] == ["p.com", "p0.com"]
    assert [r["roaster_id"] for r in out["quarantined"]] == ["q.com"]
    # While woocommerce still crawls, its stores and partial fetches wait for its pass to end.
    (tmp_path / "r").mkdir()
    out = fetchhealth.judge(_db(tmp_path / "r", rc, {}), _results(), running=frozenset({"woocommerce"}))
    assert [e["roaster_id"] for e in out["repull"]] == ["sg2.com"] and out["partial"] == []
