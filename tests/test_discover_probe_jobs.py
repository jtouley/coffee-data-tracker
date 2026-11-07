"""The discover and probe Cloud Run jobs: they record new roasters, keep what they found when a part fails or the
time runs out, publish it, and leave job_manifests rows (src/terraform/ingestion.tf, docs/ingestion/RUNBOOK.md §5.7).

Each test names the cause it guards: the production jobs ran from 2026-11-02 and left nothing in the lake."""

from __future__ import annotations

import argparse
import re
import types
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from coffeedata import cli, control, discover, registry
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


def test_the_seed_was_probed_2026_10_26_so_no_roaster_is_stale_until_2026_11_30():
    """Confirmed cause: every seed roaster's last_probed is 2026-10-26, so `--stale-days 28` selects none of them
    on 2026-11-02 (cutoff 2026-10-05). With discovery never run, the registry added nobody either: the probe had
    nothing to do, wrote nothing and exited 0."""
    import csv

    with (ROOT / "config/roasters.csv").open() as f:
        probed = {r["last_probed"] for r in csv.DictReader(f)}
    assert max(probed) >= "2026-10-26" and min(probed) == "2026-10-26"
    states = [types.SimpleNamespace(status="pending", last_probed=d, roaster=types.SimpleNamespace(roaster_id=d))
              for d in sorted(probed)]  # fmt: skip
    for day in ("2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"):
        assert cli.probe_todo(states, day, 28) == []
    assert cli.probe_todo(states, "2026-11-30", 28) == ["2026-10-26"]


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


def test_osm_records_the_states_overpass_did_not_answer(lake, monkeypatch):
    monkeypatch.setattr(discover, "_overpass", lambda client, st: None if st == "CA" else [
        {"tags": {"craft": "coffee_roaster", "website": f"https://{st.lower()}roast.example/", "name": st}}
    ])  # fmt: skip
    out = cli.discover_source(lake, "osm", states=["CA", "OR"])
    assert (out["recorded"], out["failed_states"]) == (1, ["CA"])
    (end,) = _jobs(lake, "discover")
    assert (end["status"], end["ok"], end["errors"]) == ("done", 1, 1)


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
    monkeypatch.setattr(discover, "_overpass", lambda client, st: asked.append(st))  # None: not answered
    failed: list[str] = []
    assert discover.osm(failed=failed) == []
    assert asked == discover.US_STATES[: discover.OVERPASS_DOWN_AFTER]
    assert failed == discover.US_STATES  # every state is recorded as failed, asked or not

    clock = [0.0]
    monkeypatch.setattr(discover.time, "monotonic", lambda: clock[0])

    def slow(client, st):
        clock[0] += 1000
        return []

    monkeypatch.setattr(discover, "_overpass", slow)
    failed = []
    discover.osm(["AL", "AK", "AZ", "AR", "CA"], failed=failed, budget_s=2500)
    assert failed == ["AR", "CA"]
