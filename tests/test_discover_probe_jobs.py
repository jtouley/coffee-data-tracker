"""The discover and probe Cloud Run jobs: they record new roasters, keep what they found when a part fails or the
time runs out, publish it, and leave job_manifests rows (src/terraform/ingestion.tf, docs/ingestion/RUNBOOK.md §5.7).

Each test names the cause it guards: the production jobs ran from 2026-11-02 and left nothing in the lake."""

from __future__ import annotations

import argparse
import json
import re
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from coffeedata import cli, connectors, control, discover, registry
from coffeedata.http import DeadlineReached
from coffeedata.lake import Lake

ROOT = Path(__file__).parents[1]
OK = {"status": "ok", "final_host": "", "platform": "shopify", "http_status": 200}


def _jobs(lake: Lake, job: str) -> list[dict]:
    return [r for r in control.rows(lake) if r["job"] == job]


def _probe_rows(lake: Lake) -> list[dict]:
    files = sorted((lake.root / "probes").glob("run_date=2*/*.parquet"))
    return [r for f in files for r in pq.read_table(f).to_pylist()]


def _registry_row(rid: str) -> dict:
    return {"roaster_id": rid, "name": rid, "city": "", "state": "OR", "locations": 1, "source": "osm",
            "added_at": datetime(2026, 11, 1, tzinfo=UTC), "opted_out": False}  # fmt: skip


def _probe_args(**kw) -> argparse.Namespace:
    base = {"stale_days": 28, "only": None, "sample": None, "workers": 2, "run_date": "2026-11-02",
            "deadline_min": None}  # fmt: skip
    return argparse.Namespace(**(base | kw))


# --- probe: the Nov 2 run had nothing to probe, and said nothing -----------------------------------------------------


def test_a_roaster_probed_exactly_stale_days_ago_waits_a_week():
    """Confirmed cause of the Nov 2 run doing nothing: the seed's roasters were probed 2026-10-26 (a few 2026-11-02),
    so `--stale-days 28` selected none of them until 2026-11-30. The cutoff is strict: a roaster is stale when it was
    probed *more than* 28 days before, so one probed exactly 28 days earlier waits for the next week. Fixed states,
    so a seed refresh doesn't break this."""
    states = [types.SimpleNamespace(status="pending", last_probed=d, roaster=types.SimpleNamespace(roaster_id=d))
              for d in ("2026-10-26", "2026-11-02")]  # fmt: skip
    for day in ("2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"):
        assert cli.probe_todo(states, day, 28) == []  # Nov 23 is exactly 28 days after Oct 26
    assert cli.probe_todo(states, "2026-11-30", 28) == ["2026-10-26"]
    assert cli.probe_todo(states, "2026-12-07", 28) == ["2026-10-26", "2026-11-02"]


def test_a_probe_with_nothing_to_do_still_records_that_it_ran(lake, monkeypatch):
    monkeypatch.setattr(discover, "probe_one", lambda c, d: pytest.fail("nothing is stale"))
    cli.cmd_probe(_probe_args())
    (end,) = _jobs(lake, "probe")
    assert (end["status"], end["todo"], end["run_date"]) == ("done", 0, date(2026, 11, 2))
    assert _probe_rows(lake) == []


def test_a_roaster_discovery_added_is_probed_first_and_its_row_lands(lake, monkeypatch):
    """discover -> probe: a registry row has no last_probed, so the next probe asks it, before the stale ones."""
    lake.write("registry", "registry/source=osm/2026-11-01-x.parquet", [_registry_row("newroaster.example")])
    asked = []
    monkeypatch.setattr(discover, "probe_one", lambda c, d: asked.append(d) or OK | {"final_host": d})
    cli.cmd_probe(_probe_args())
    assert asked == ["newroaster.example"]  # the 3,200 seed roasters were probed 2026-10-26: not stale yet
    (row,) = _probe_rows(lake)
    assert (row["roaster_id"], row["status"], row["run_date"]) == (
        "newroaster.example",
        "ok",
        date(2026, 11, 2),
    )
    (end,) = _jobs(lake, "probe")
    assert (end["status"], end["todo"], end["ok"], end["skipped"]) == ("done", 1, 1, 0)


def test_probe_order_is_never_probed_first_then_longest_unprobed():
    def s(rid, last, status="active"):
        return types.SimpleNamespace(
            status=status, last_probed=last, roaster=types.SimpleNamespace(roaster_id=rid)
        )

    states = [s("b.com", "2026-09-01"), s("a.com", "2026-08-01"), s("new.com", None), s("fresh.com", "2026-11-20"),
              s("gone.com", None, status="opted_out")]  # fmt: skip
    assert cli.probe_todo(states, "2026-11-30", 28) == ["new.com", "a.com", "b.com"]


# --- probe: results are kept when the run stops early ----------------------------------------------------------------


def test_probe_hands_over_each_batch_as_it_finishes_and_the_rest_on_a_crash(monkeypatch):
    """Before: every result was held in memory and written once, at the end, so a crash, an OOM kill or the
    240-minute timeout lost the whole run."""

    def one(client, d):
        if d == "boom.com":
            raise SystemExit(
                "killed"
            )  # not an Exception: _safe_probe lets it through, as it would a real abort
        return OK

    monkeypatch.setattr(discover, "probe_one", one)
    batches: list[list[str]] = []
    with pytest.raises(SystemExit):
        discover.probe(["a.com", "b.com", "c.com", "boom.com", "d.com"], workers=1, batch_size=2,
                       on_batch=lambda rows: batches.append([r["roaster_id"] for r in rows]))  # fmt: skip
    assert batches == [["a.com", "b.com"], ["c.com"]]


def test_probe_stops_starting_roasters_at_its_deadline(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(discover.time, "monotonic", lambda: clock[0])

    def one(client, d):
        clock[0] += 10
        return OK

    monkeypatch.setattr(discover, "probe_one", one)
    rows = discover.probe([f"{i}.com" for i in range(6)], workers=1, deadline_s=25)
    assert [r["roaster_id"] for r in rows] == ["0.com", "1.com", "2.com"]


def test_a_roaster_cut_off_by_the_deadline_gets_no_row():
    """A request the deadline stopped is no answer about the site: before, any exception became `unreachable`,
    which counts as probed for 28 days."""

    class Client:
        def get(self, url, **kw):
            raise DeadlineReached("the run's deadline passed")

    assert discover._safe_probe(Client(), "slow.com") is None  # type: ignore[arg-type]


def test_cmd_probe_ends_deadline_with_the_count_left(lake, monkeypatch):
    for i in range(3):
        lake.write(
            "registry", f"registry/source=osm/2026-11-01-{i}.parquet", [_registry_row(f"n{i}.example")]
        )
    monkeypatch.setattr(discover, "probe", lambda todo, **kw: [
        {"run_date": date(2026, 11, 2), "probed_at": datetime(2026, 11, 2, 8, tzinfo=UTC), "roaster_id": todo[0], **OK}
    ])  # fmt: skip
    cli.cmd_probe(_probe_args(deadline_min=100))
    (end,) = _jobs(lake, "probe")
    assert (end["status"], end["todo"], end["ok"], end["skipped"]) == ("deadline", 3, 1, 2)


def test_a_failed_probe_says_so_in_its_end_row(lake, monkeypatch):
    def broken(lake):
        raise OSError("HTTP 503")

    monkeypatch.setattr(registry, "current", broken)
    with pytest.raises(OSError):
        cli.cmd_probe(_probe_args())
    (end,) = _jobs(lake, "probe")
    assert end["status"] == "failed" and "HTTP 503" in control.detail_of(end)["error"]


# --- discover: one source failing never loses the other ---------------------------------------------------------------


def _overture_rows(release):
    return [{"roaster_id": "roastery.example", "name": "Roastery", "city": "Bend", "state": "OR", "locations": 1,
             "confidence": 0.9, "tagged_roastery": True, "roast_in_name": True, "source": f"overture-{release}"}]  # fmt: skip


def test_overpass_down_still_records_overture_and_fails_the_execution(lake, monkeypatch):
    """Before: `discover overture; discover osm; publish` in one shell. Overture runs first in one 4Gi process, so
    a kill there took OSM's rows with it, and an all-states Overpass outage recorded 0 rows and exited 0."""
    monkeypatch.setattr(discover, "overture_latest_release", lambda: "2026-10-21.0")
    monkeypatch.setattr(discover, "overture", _overture_rows)

    def down(states, failed):
        failed.extend(states)
        return []

    monkeypatch.setattr(discover, "osm", down)
    with pytest.raises(SystemExit, match="discover failed: osm"):
        cli.cmd_discover(
            argparse.Namespace(source=["osm", "overture"], release=None, states=None, publish=False)
        )
    files = list((lake.root / "registry").glob("source=overture/*.parquet"))
    assert [r["roaster_id"] for r in pq.read_table(files[0]).to_pylist()] == ["roastery.example"]
    ends = {r["connector"]: r for r in _jobs(lake, "discover")}
    assert ends["osm"]["status"] == "failed" and "Overpass" in control.detail_of(ends["osm"])["error"]
    assert (ends["overture"]["status"], ends["overture"]["ok"]) == ("done", 1)
    assert control.detail_of(ends["overture"])["new"] == 1  # not in the seed


def test_an_overture_scan_that_finds_nothing_fails(lake, monkeypatch):
    monkeypatch.setattr(discover, "overture", lambda release: [])
    with pytest.raises(RuntimeError, match="found no roasters"):
        cli.discover_source(lake, "overture", "2026-10-21.0")
    (end,) = _jobs(lake, "discover")
    assert end["status"] == "failed"
    assert not list((lake.root / "registry").glob("source=overture/*.parquet"))


def test_osm_records_the_states_overpass_did_not_answer(lake, monkeypatch, caplog):
    caplog.set_level("WARNING")
    monkeypatch.setattr(discover, "_overpass", lambda client, st, stop=None: None if st == "CA" else [
        {"tags": {"craft": "coffee_roaster", "website": f"https://{st.lower()}roast.example/", "name": st}}
    ])  # fmt: skip
    out = cli.discover_source(lake, "osm", states=["CA", "OR"])
    assert (out["recorded"], out["failed_states"]) == (1, ["CA"])
    (end,) = _jobs(lake, "discover")
    assert (end["status"], end["ok"], end["errors"]) == ("done", 1, 1)
    assert "Overpass didn't answer 1 states: CA" in caplog.text  # a partial outage is logged at WARNING


def test_overpass_is_asked_again_when_busy_and_a_timed_out_query_is_not_an_answer():
    answers = iter([httpx.Response(429), httpx.Response(504),
                    httpx.Response(200, json={"elements": [{"tags": {}}]})])  # fmt: skip
    waits: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: next(answers)))
    assert discover._overpass(client, "CA", sleep=waits.append) == [{"tags": {}}]
    assert waits == list(discover.OVERPASS_WAITS)

    # a query that ran out of time answers 200 with a runtime-error remark and only some elements
    remark = {"elements": [{"tags": {}}], "remark": 'runtime error: Query timed out in "query" at line 1'}
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=remark)))
    assert discover._overpass(client, "TX", sleep=lambda s: None) is None

    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(400)))
    assert discover._overpass(client, "TX", sleep=lambda s: pytest.fail("a 400 isn't asked again")) is None


def test_the_overture_scan_runs_with_bounded_threads_and_memory(monkeypatch):
    """Unbounded, the scan peaked at 2.7 GiB resident locally (4 threads) in a 4Gi job; bounded, 1.5 GiB."""
    import duckdb

    seen = {}

    def connect(*a, config=None, **kw):
        seen.update(config or {})
        raise RuntimeError("stop here")

    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: types.SimpleNamespace(read=lambda: b"<Key>x.parquet</Key>"))  # fmt: skip
    with pytest.raises(RuntimeError, match="stop here"):
        discover.overture("2026-10-21.0")
    assert seen == {"threads": 2, "memory_limit": "1GB"}


def test_cloud_runs_retry_skips_a_source_the_first_attempt_recorded(lake, monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "discover-abc12")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    monkeypatch.setattr(discover, "osm", lambda states, failed: [_registry_row("a.example")])
    cli.discover_source(lake, "osm")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    monkeypatch.setattr(discover, "osm", lambda states, failed: pytest.fail("recorded already"))
    assert cli.discover_source(lake, "osm") == {"source": "osm", "skipped": True}
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "discover-other")  # a new execution records again
    monkeypatch.setattr(discover, "osm", lambda states, failed: [_registry_row("a.example")])
    assert cli.discover_source(lake, "osm")["recorded"] == 1


# --- publish: what the jobs record is what goes to the dataset ---------------------------------------------------------


def test_discover_publishes_each_source_with_its_job_rows(tmp_path, monkeypatch):
    commits: list[list[str]] = []

    class FakeApi:
        def __init__(self, token=None):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            return []

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            commits.append(sorted(op.path_in_repo for op in operations))

        def list_repo_tree(self, *a, **kw):
            return []

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    monkeypatch.setenv("COFFEEDATA_LAKE", "hf://datasets/someone/coffee")
    monkeypatch.setattr(Lake, "remote_has", lambda self, table: False)  # read staging alone (no network)
    monkeypatch.setattr(discover, "overture_latest_release", lambda: "2026-10-21.0")
    monkeypatch.setattr(discover, "overture", _overture_rows)
    monkeypatch.setattr(discover, "osm", lambda states, failed: [_registry_row("osm.example")])
    cli.cmd_discover(argparse.Namespace(source=["osm", "overture"], release=None, states=None, publish=True))
    osm, overture = (
        sorted(p for p in c if not p.endswith("schema.parquet") and "=schema" not in p) for c in commits
    )
    assert [p.split("/")[0] for p in osm] == ["job_manifests", "job_manifests", "registry"]
    assert re.fullmatch(r"registry/source=osm/\d{4}-\d\d-\d\d-.+\.parquet", osm[-1])
    assert overture[-1].startswith("registry/source=overture/2026-10-21.0-")
    assert not [p for p in (tmp_path / "staging").rglob("*.parquet") if "schema" not in p.name]  # all sent


# --- the jobs' commands ---------------------------------------------------------------------------------------------


def _job_args(name: str) -> str:
    tf = (ROOT / "src/terraform/ingestion.tf").read_text()
    block = tf[tf.index(f'"{name}" = {{') :]
    return block[block.index("args") : block.index("egress")]


def test_the_discover_job_publishes_each_source_and_exits_non_zero_on_a_failure():
    args = _job_args("discover")
    assert "coffeedata discover osm overture --publish || rc=1" in args
    assert 'coffeedata publish --message \\"discover\\" || rc=1; exit $rc' in args


def test_the_probe_job_publishes_whatever_the_probe_exit_and_ends_before_the_crawls():
    args = _job_args("probe")
    assert (
        "|| rc=1; coffeedata publish" in args and "&&" not in args
    )  # `&&` skipped the publish on any failure
    deadline = float(re.search(r"--deadline-min (\d+)", args).group(1))  # type: ignore[union-attr]
    tf = (ROOT / "src/terraform/ingestion.tf").read_text()
    probe_at = re.search(r'"probe"\s+= \{ job = "probe", cron = "17 (\d+) \* \* 1" \}', tf)
    crawl_at = re.search(r'"crawl-shopify"\s+= \{ job = "crawl-shopify", cron = "17 (\d+) \* \* 1" \}', tf)
    assert probe_at and crawl_at
    assert deadline < 60 * (int(crawl_at.group(1)) - int(probe_at.group(1)))
    assert deadline < 240  # the task timeout (14400s)


def test_an_overpass_outage_stops_after_three_states_and_a_slow_pass_after_its_budget(monkeypatch):
    """OSM runs first in the job: unbounded, an outage (each state up to 3 x 90 s timeouts plus 2 minutes of
    waits, 52 states) would run past the 240-minute task timeout, and Overture would never run."""
    asked: list[str] = []
    monkeypatch.setattr(
        discover, "_overpass", lambda client, st, stop=None: asked.append(st)
    )  # None: not answered
    failed: list[str] = []
    assert discover.osm(failed=failed) == []
    assert asked == discover.US_STATES[: discover.OVERPASS_DOWN_AFTER]
    assert failed == discover.US_STATES  # every state is recorded as failed, asked or not

    clock = [0.0]
    monkeypatch.setattr(discover.time, "monotonic", lambda: clock[0])

    def slow(client, st, stop=None):
        clock[0] += 1000
        return []

    monkeypatch.setattr(discover, "_overpass", slow)
    failed = []
    discover.osm(["AL", "AK", "AZ", "AR", "CA"], failed=failed, budget_s=2500)
    assert failed == ["AR", "CA"]


# --- review fixes --------------------------------------------------------------------------------------------------


def test_an_outage_after_the_first_states_fails_the_source_but_keeps_what_was_answered(lake, monkeypatch):
    """Overpass answers AL, then stops: before, anything short of all states unanswered ended `done` and exit 0."""
    monkeypatch.setattr(discover, "_overpass", lambda client, st, stop=None: [
        {"tags": {"craft": "coffee_roaster", "website": "https://alroast.example/"}}
    ] if st == "AL" else None)  # fmt: skip
    with pytest.raises(RuntimeError, match="51 of 52 states unanswered"):
        cli.discover_source(lake, "osm")
    (end,) = _jobs(lake, "discover")
    assert (end["status"], end["ok"], end["errors"]) == ("failed", 1, 51)
    files = list((lake.root / "registry").glob("source=osm/*.parquet"))
    assert [r["roaster_id"] for r in pq.read_table(files[0]).to_pylist()] == ["alroast.example"]


# The retry tests run on a frozen clock (control.now): 00:30 UTC on a Monday, so the first attempt began on the
# previous day and in the previous ISO week (its row still belongs to this run's week), and 12:00 UTC midweek.
# They once wrote the first attempt under a hard-coded week and read the real clock, so they failed once the
# calendar moved past that week.
RETRY_CLOCKS = [datetime(2026, 11, 9, 0, 30, tzinfo=UTC), datetime(2026, 11, 11, 12, 0, tzinfo=UTC)]


def _first_attempt(lake, monkeypatch, frozen: datetime, execution: str) -> str:
    """Freeze the clock, write the first attempt's running row (begun 90 minutes ago, in this run's week) and
    return the run date the retry is given."""
    monkeypatch.setattr(control, "now", lambda: frozen)
    run_date = frozen.date().isoformat()
    first = {"run_date": connectors.week_of(run_date), "job": "probe", "connector": None, "attempt_id": "a0",
             "started_at": frozen - timedelta(minutes=90),
             "status": "running", "detail": {"execution": execution, "task_attempt": 0}}  # fmt: skip
    control.write(lake, first)
    return run_date


@pytest.mark.parametrize("frozen", RETRY_CLOCKS, ids=["monday-00:30", "midweek-12:00"])
def test_a_probe_retry_keeps_the_first_attempts_window(lake, monkeypatch, frozen):
    """Cloud Run retries a failed probe once: the retry must not get a fresh 100 minutes and run into the crawls."""
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "probe-x1")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    run_date = _first_attempt(lake, monkeypatch, frozen, "probe-x1")
    seen = {}
    monkeypatch.setattr(discover, "probe", lambda todo, **kw: seen.update(kw) or [])
    cli.cmd_probe(_probe_args(run_date=run_date, deadline_min=100))
    assert 0 < seen["deadline_s"] <= 10 * 60 + 5  # what's left of the first attempt's 100 minutes


@pytest.mark.parametrize("frozen", RETRY_CLOCKS, ids=["monday-00:30", "midweek-12:00"])
def test_a_probe_retry_does_not_take_its_own_setup_off_the_window_twice(lake, monkeypatch, frozen):
    """The retry's window is measured from the first attempt's start up to now; this attempt's setup (5 minutes
    here) is inside that already. Before, it was subtracted again and the retry stopped early by the setup time."""
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "probe-x2")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    run_date = _first_attempt(lake, monkeypatch, frozen, "probe-x2")
    clock = iter([1000.0])  # t0, then every later reading is 5 minutes on
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=lambda: next(clock, 1300.0)))
    seen = {}
    monkeypatch.setattr(discover, "probe", lambda todo, **kw: seen.update(kw) or [])
    cli.cmd_probe(_probe_args(run_date=run_date, deadline_min=100))
    assert seen["deadline_s"] == pytest.approx(10 * 60, abs=5)  # not 5 minutes


def test_a_first_probe_attempt_takes_its_setup_off_the_deadline(lake, monkeypatch):
    monkeypatch.delenv("CLOUD_RUN_TASK_ATTEMPT", raising=False)
    clock = iter([1000.0])
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=lambda: next(clock, 1300.0)))
    seen = {}
    monkeypatch.setattr(discover, "probe", lambda todo, **kw: seen.update(kw) or [])
    cli.cmd_probe(_probe_args(run_date=control.now().date().isoformat(), deadline_min=100))
    assert seen["deadline_s"] == pytest.approx(95 * 60)


def test_a_failed_batch_write_is_tried_again_and_the_original_error_is_raised(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", lambda client, d: OK)
    written: list[list[str]] = []
    calls = [0]

    def write(rows):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("staging mount hiccup")
        written.append([r["roaster_id"] for r in rows])

    with pytest.raises(OSError, match="hiccup"):
        discover.probe(["a.com", "b.com", "c.com"], workers=1, batch_size=2, on_batch=write)
    assert written == [["a.com", "b.com"]]  # the batch that failed to write, written by the flush


def test_probe_flushes_by_time_so_a_full_run_writes_few_files(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(discover.time, "monotonic", lambda: clock[0])
    real = discover.as_completed

    def paced(futures):  # each result is seen two minutes after the one before
        for f in real(futures):
            clock[0] += 120
            yield f

    monkeypatch.setattr(discover, "as_completed", paced)
    monkeypatch.setattr(discover, "probe_one", lambda client, d: OK)
    batches: list[int] = []
    discover.probe(
        [f"{i}.com" for i in range(12)], workers=1, on_batch=lambda rows: batches.append(len(rows))
    )
    assert batches == [5, 5, 2]  # every 10 minutes, then the rest
    assert discover.PROBE_BATCH >= 1000 and discover.PROBE_FLUSH_S == 600


def test_overpass_gives_up_rather_than_wait_past_the_budget(monkeypatch):
    monkeypatch.setattr(discover.time, "monotonic", lambda: 100.0)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429)))
    assert (
        discover._overpass(client, "CA", sleep=lambda s: pytest.fail("no wait past the budget"), stop=120.0)
        is None
    )


def test_the_weekly_run_discovers_osm_first_under_its_own_week(lake, monkeypatch):
    calls = []
    for name in ("cmd_probe", "cmd_ingest", "cmd_enrich", "cmd_classify", "cmd_build", "cmd_publish",
                 "cmd_export_public", "cmd_load_snowflake", "cmd_site"):  # fmt: skip
        monkeypatch.setattr(cli, name, lambda a: True)
    monkeypatch.setattr(cli, "discover_source", lambda lake, src, week=None: calls.append((src, week)))
    monkeypatch.delenv("PUBLIC_EXPORT_URL", raising=False)
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    cli.cmd_weekly(argparse.Namespace(run_date="2026-11-04", discover=False, sample=None, enrich_limit=None,
                                      db="build/coffee.duckdb", out="build/site", accept_volume_change=False))  # fmt: skip
    assert calls == [("osm", "2026-11-02"), ("overture", "2026-11-02")]


def test_a_weekly_rerun_asks_again_a_source_whose_discover_failed(lake, monkeypatch):
    """An OSM outage still records the states Overpass answered, so the registry has OSM rows for the month; before,
    a re-run of the week took those as "done for the month" and skipped OSM without --discover."""
    calls = []
    for name in ("cmd_probe", "cmd_ingest", "cmd_enrich", "cmd_classify", "cmd_build", "cmd_publish",
                 "cmd_export_public", "cmd_load_snowflake", "cmd_site"):  # fmt: skip
        monkeypatch.setattr(cli, name, lambda a: True)
    monkeypatch.delenv("PUBLIC_EXPORT_URL", raising=False)
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    # The outage's leftovers: what Overpass answered is in the registry, and the source's end row says failed. The
    # Cloud Run job ran on Sunday the 1st, so its rows are under the week before this run's (2026-10-26).
    lake.write("registry", "registry/source=osm/2026-11-01-b1.parquet", [_registry_row("alroast.example")])
    job = control.start(lake, "discover", "2026-10-26", "osm")
    control.finish(lake, job, "failed", ok=1, errors=51)
    # Overture's attempt was killed after recording: only its start row, older than the job's 4 h timeout.
    lake.write("registry", "registry/source=overture/latest-b2.parquet",
               [_registry_row("ovroast.example") | {"source": "overture"}])  # fmt: skip
    control.write(lake, {"run_date": "2026-10-26", "job": "discover", "connector": "overture", "attempt_id": "killed",
                         "started_at": control.now() - timedelta(hours=5), "status": "running"})  # fmt: skip
    monkeypatch.setattr(cli, "Lake", lambda: lake)
    monkeypatch.setattr(cli, "discover_source", lambda lake, src, week=None: calls.append(src))
    args = argparse.Namespace(run_date="2026-11-04", discover=False, sample=None, enrich_limit=None,
                              db="build/coffee.duckdb", out="build/site", accept_volume_change=False)  # fmt: skip
    cli.cmd_weekly(args)
    assert calls == ["osm", "overture"]
    # Once each has a done row, the month is done.
    for src in ("osm", "overture"):  # a second later than the rows above, so they are the latest
        control.write(lake, {"run_date": "2026-11-02", "job": "discover", "connector": src, "attempt_id": f"ok-{src}",
                             "started_at": control.now() + timedelta(seconds=1), "status": "done"})  # fmt: skip
    calls.clear()
    cli.cmd_weekly(args)
    assert calls == []
    # A discover still in progress (started minutes ago, say the Cloud Run job) isn't started again alongside it.
    later = control.now() + timedelta(seconds=2)
    control.write(lake, {"run_date": "2026-11-02", "job": "discover", "connector": "osm", "attempt_id": "in-progress",
                         "started_at": later, "status": "running"})  # fmt: skip
    control.write(lake, {**control.start(lake, "discover", "2026-11-02", "overture"), "attempt_id": "new-failed",
                         "started_at": later, "ended_at": later, "status": "failed"})  # fmt: skip
    cli.cmd_weekly(args)
    assert calls == ["overture"]  # the failed one is asked again; the running one is left to finish


def test_the_merges_rollup_skips_a_file_another_jobs_publish_removed(lake, monkeypatch):
    """When the 1st is a Tuesday, discover (03:17) and the merge (03:17) share the week's job_manifests partition in
    staging. Discover's publish uploads and removes its row files; one that goes between the merge's glob and its
    read or unlink made rollup raise, after the merge's `done` row and before its publish."""
    for job in ("merge", "discover", "probe"):
        control.finish(lake, control.start(lake, job, "2026-11-30"), "done")
    d = lake.root / "job_manifests" / "run_date=2026-11-30"
    files = sorted(d.glob("attempt=*.parquet"))
    assert len(files) == 6
    real = pq.read_table

    def published_meanwhile(f, *a, **kw):
        if f == files[0]:
            files[1].unlink()  # discover's publish sent and removed it: its rows are in the dataset
        return real(f, *a, **kw)

    monkeypatch.setattr(control.pq, "read_table", published_meanwhile)
    assert control.rollup(lake, "2026-11-30") == 5  # the five rows it could read, folded into one file
    (left,) = d.glob("attempt=*.parquet")
    assert left.name.startswith("attempt=rollup-")


def test_rollup_skips_a_file_gcsfuse_reports_stale(lake, monkeypatch):
    """On the gcsfuse staging mount, a file removed after it was opened can raise ESTALE rather than ENOENT."""
    import errno

    for job in ("merge", "discover"):
        control.finish(lake, control.start(lake, job, "2026-11-30"), "done")
    d = lake.root / "job_manifests" / "run_date=2026-11-30"
    files = sorted(d.glob("attempt=*.parquet"))
    real = pq.read_table

    def stale(f, *a, **kw):
        if f == files[0]:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real(f, *a, **kw)

    monkeypatch.setattr(control.pq, "read_table", stale)
    assert control.rollup(lake, "2026-11-30") == 3
    monkeypatch.setattr(
        control.pq, "read_table", lambda f, *a, **kw: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    for job in ("probe", "merge"):
        control.finish(lake, control.start(lake, job, "2026-11-30"), "done")
    with pytest.raises(OSError, match="io"):  # any other error still stops the merge
        control.rollup(lake, "2026-11-30")


# --- probe_one: which platform serves the catalog (rules audit #94) -------------------------------------------------


def _probe(routes: dict[str, httpx.Response | Exception], domain: str = "a.com") -> dict:
    """probe_one over mocked hosts: `routes` maps a URL regex to its answer; robots.txt allows everything and any
    other URL is a 404."""
    import respx

    from coffeedata.http import PoliteClient

    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots\.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\n")
        )
        for pattern, answer in routes.items():
            route = m.get(url__regex=pattern)
            if isinstance(answer, Exception):
                route.mock(side_effect=answer)
            else:
                route.mock(return_value=answer)
        m.get(url__regex=r".*").mock(return_value=httpx.Response(404))
        with PoliteClient(min_interval=0, retries=1) as c:
            return discover.probe_one(c, domain)


HOME = r"https://a\.com/$"


@pytest.mark.parametrize(
    "routes,want",
    [
        # the JSON endpoints are asked first: Shopify's products.json, then WooCommerce's Store API
        ({HOME: httpx.Response(200, text="<html>woocommerce</html>"),
          r"/products\.json": httpx.Response(200, json={"products": []})}, "shopify"),
        ({HOME: httpx.Response(200, text="<html></html>"),
          r"/wp-json/wc/store/v1/products": httpx.Response(200, json=[])}, "woocommerce"),
        # a products.json without "products", or a Store API answering an object, isn't that platform
        ({HOME: httpx.Response(200, text="<html></html>"),
          r"/products\.json": httpx.Response(200, json={"items": []}),
          r"/wp-json/wc/store/v1/products": httpx.Response(200, json={"code": "x"})}, "other"),
        # then the home page's markers, first match in HTML_MARKERS order
        ({HOME: httpx.Response(200, text='<script src="//cdn.shopify.com/x.js"></script>')}, "shopify?"),
        ({HOME: httpx.Response(200, text="<p>Powered by Squarespace</p> wixstatic")}, "squarespace?"),
        ({HOME: httpx.Response(200, text="<p>BigCommerce</p>")}, "bigcommerce?"),
        ({HOME: httpx.Response(200, text='<img src="https://static.wixstatic.com/a.png">')}, "wix?"),
        ({HOME: httpx.Response(200, text='<body class="woocommerce-page">')}, "woocommerce?"),
        ({HOME: httpx.Response(200, text='<link href="https://cdn.editmysite.com/x.css">')}, "square_online?"),
        ({HOME: httpx.Response(200, text="<html data-wf-site=1>webflow</html>")}, "webflow?"),
        ({HOME: httpx.Response(200, text="<html>plain</html>")}, "other"),
    ],
)  # fmt: skip
def test_probe_detects_the_platform(routes, want):
    out = _probe(routes)
    assert (out["status"], out["platform"], out["final_host"], out["http_status"]) == (
        "ok",
        want,
        "a.com",
        200,
    )


def test_probe_reports_a_non_200_home_page_as_http_status():
    out = _probe({HOME: httpx.Response(404)})
    assert out == {"status": "http_404", "final_host": "a.com", "platform": "", "http_status": 404}


def test_probe_tries_www_when_the_bare_host_does_not_answer():
    out = _probe(
        {HOME: httpx.ConnectError("refused"), r"https://www\.a\.com/$": httpx.Response(200, text="x")}
    )
    assert (out["status"], out["final_host"], out["platform"]) == ("ok", "www.a.com", "other")


def test_probe_with_no_host_answering_is_unreachable():
    out = _probe({r"https://(www\.)?a\.com/$": httpx.ConnectError("refused")})
    assert out == {"status": "unreachable", "final_host": "", "platform": "", "http_status": None}


# --- discovery filters: Overture (rules audit #92) and OpenStreetMap (#93) ------------------------------------------


def _overture_place(name, cat="coffee_roastery", alts=(), x=-122.6, y=45.5, country="US", website="https://r.com",
                    basic=None, conf=0.9, city="Portland", region="OR"):  # fmt: skip
    return {"names": {"primary": name}, "taxonomy": {"primary": cat, "alternates": list(alts)},
            "basic_category": basic, "bbox": {"xmin": x, "ymin": y}, "confidence": conf,
            "addresses": [{"locality": city, "region": region, "country": country}],
            "websites": [website] if website else None}  # fmt: skip


def test_overture_filter_keeps_us_roasteries_with_a_storefront(tmp_path, monkeypatch):
    """The scan's SQL over a local file shaped like Overture's places: in a US box and addressed in the US, with a
    website, and tagged coffee_roastery (primary or alternate) or named "roast" with coffee in its category or name.
    Websites are keyed by normalized domain, and only storefront hosts count."""
    import duckdb

    places = [
        _overture_place("Tagged", website="https://tagged.com"),
        _overture_place("Alt", cat="cafe", alts=["coffee_roastery"], website="https://alt.com"),
        _overture_place("Beanery Roasters", cat="coffee_shop", website="https://roastcat.com"),
        _overture_place("Sunrise Roasting", cat="food", basic="coffee_shop", website="https://basic.com"),
        _overture_place("Roast Coffee House", cat="restaurant", website="https://roastname.com"),
        _overture_place(
            "Roast Beef Shop", cat="restaurant", website="https://beef.com"
        ),  # no coffee anywhere
        _overture_place("Cafe", cat="cafe", website="https://cafe.com"),  # not roasting
        _overture_place(
            "Toronto", country="CA", x=-79.4, y=43.7, website="https://ca.com"
        ),  # in the box, not US
        _overture_place("Guam", x=144.8, y=13.4, website="https://guam.com"),  # US, no box
        _overture_place("Anchorage", x=-149.9, y=61.2, website="https://ak.com"),
        _overture_place("Kona", x=-155.9, y=19.6, website="https://hi.com"),
        _overture_place("No site", website=None),
        _overture_place("Square", website="https://shop.square.site"),  # not a storefront of its own
        # one domain, two places: one row, two locations, the more confident place's name
        _overture_place("Two A", website="https://www.two.com/shop", conf=0.5),
        _overture_place("Two B", website="https://two.com", conf=0.95, city="Salem"),
    ]
    src = tmp_path / "places.json"
    src.write_text("\n".join(json.dumps(p) for p in places))
    duckdb.execute(f"copy (select * from read_json('{src}')) to '{tmp_path}/part-0.parquet' (format parquet)")
    monkeypatch.setattr(discover, "OVERTURE", str(tmp_path) + "/")
    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: types.SimpleNamespace(read=lambda: b"<Key>part-0.parquet</Key>"))  # fmt: skip
    real_connect = duckdb.connect

    class NoInstall:  # the test reads a local file: skip httpfs, which needs the network the first time
        def __init__(self, con):
            self.con = con

        def sql(self, q):
            return None if q.startswith("INSTALL httpfs") else self.con.sql(q)

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: NoInstall(real_connect()))
    rows = {r["roaster_id"]: r for r in discover.overture("2026-10-21.0")}
    assert sorted(rows) == ["ak.com", "alt.com", "basic.com", "hi.com", "roastcat.com", "roastname.com",
                            "tagged.com", "two.com"]  # fmt: skip
    assert rows["two.com"]["locations"] == 2 and (rows["two.com"]["name"], rows["two.com"]["city"]) == (
        "Two B",
        "Salem",
    )
    assert rows["tagged.com"]["tagged_roastery"] and not rows["alt.com"]["tagged_roastery"]
    assert rows["roastcat.com"]["roast_in_name"] and not rows["tagged.com"]["roast_in_name"]
    assert rows["tagged.com"]["source"] == "overture-2026-10-21.0"


def test_osm_keeps_storefront_websites_once(monkeypatch):
    elements = {
        "OR": [
            {"tags": {"craft": "coffee_roaster", "name": "A", "website": "https://www.a-roast.com/"}},
            {"tags": {"craft": "coffee_roaster", "name": "A again", "website": "https://a-roast.com"}},
            {"tags": {"roastery": "yes", "name": "B", "contact:website": "https://b-roast.com"}},
            {"tags": {"craft": "coffee_roaster", "name": "C", "website": "https://c.square.site"}},
            {"tags": {"craft": "coffee_roaster", "name": "No site"}},
        ],
        "WA": [{"tags": {"craft": "coffee_roaster", "name": "A in WA", "website": "https://a-roast.com"}}],
    }
    monkeypatch.setattr(discover, "_overpass", lambda client, st, stop=None: elements[st])
    rows = discover.osm(states=["OR", "WA"])
    assert [(r["roaster_id"], r["name"], r["state"]) for r in rows] == [("a-roast.com", "A", "OR"),
                                                                        ("b-roast.com", "B", "OR")]  # fmt: skip
    assert all(r["source"] == "osm" and r["tagged_roastery"] for r in rows)
