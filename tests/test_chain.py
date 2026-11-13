"""The Tuesday chain (issue #84): enrich-week runs only after the week's merge is done, the build job (classify,
then build) only after its enrichment; each waits until a deadline and then fails loudly. And the merge, run
twice: what a second run does to the files the first one stored, and why it is never retried automatically
(src/coffeedata/chain.py, src/terraform/ingestion.tf)."""

import hashlib
import logging
import re
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from coffeedata import chain, cli, connectors, control, registry
from coffeedata.lake import Lake
from coffeedata.models import Roaster

from .lakehelp import needs_dbt, product, run

WEEK = "2026-11-02"  # a Monday
TUESDAY = "2026-11-03"
# Each precondition: the command that has it, and the job_manifests `job` that must be done before it.
PRECONDITIONS = [("enrich-week", "merge"), ("classify", "enrich"), ("build", "enrich")]


class Clock:
    """A monotonic clock that only moves when the code under test sleeps: no test waits for real."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _attempt(lake: Lake, job: str, status: str | None, at: datetime | None = None) -> dict:
    """One attempt of `job` this week: a start row, and an end row unless status is None (still running, or dead)."""
    row = control.start(lake, job, WEEK)
    if at:
        row = row | {"started_at": at}
        control.write(lake, row)
    return control.finish(lake, row, status) if status else row


# --- the five cases, for each predecessor -------------------------------------------------------------------------


@pytest.mark.parametrize("job", ["merge", "enrich"])
def test_a_done_predecessor_passes_at_once(lake, job):
    _attempt(lake, job, "done")
    clock = Clock()
    out = chain.require(lake, WEEK, job, wait_s=5400, sleep=clock.sleep, clock=clock)
    assert out == {"after": job, "status": "done", "waited_s": 0} and clock.sleeps == []


@pytest.mark.parametrize("job", ["merge", "enrich"])
def test_a_failed_predecessor_fails_the_job(lake, job):
    _attempt(lake, job, "failed")
    clock = Clock()
    with pytest.raises(chain.PreconditionFailed, match=rf"`{job}` is not done.*latest attempt: failed"):
        chain.require(lake, WEEK, job, wait_s=0, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == []


@pytest.mark.parametrize("job", ["merge", "enrich"])
def test_a_missing_predecessor_fails_the_job(lake, job):
    _attempt(lake, "crawl", "done")  # the week has rows, none of them the predecessor's
    other = control.start(lake, job, "2026-10-26")  # last week's doesn't count
    control.finish(lake, other, "done")
    clock = Clock()
    with pytest.raises(chain.PreconditionFailed, match="latest attempt: missing"):
        chain.require(lake, WEEK, job, wait_s=0, sleep=clock.sleep, clock=clock)


@pytest.mark.parametrize("job", ["merge", "enrich"])
def test_a_running_predecessor_is_waited_for_until_it_finishes(lake, job, caplog):
    row = _attempt(lake, job, None)
    clock = Clock()

    def sleep(s: float) -> None:
        clock.sleep(s)
        if len(clock.sleeps) == 3:  # it ends during the third poll
            control.finish(lake, row, "done")

    with caplog.at_level(logging.WARNING):
        out = chain.require(lake, WEEK, job, wait_s=5400, sleep=sleep, clock=clock)
    assert out == {"after": job, "status": "done", "waited_s": 180}
    assert clock.sleeps == [chain.POLL_S] * 3
    assert f"waiting for this week's ({WEEK}) {job}: its latest attempt is running" in caplog.text


@pytest.mark.parametrize("job", ["merge", "enrich"])
def test_a_predecessor_still_running_at_the_deadline_fails_the_job(lake, job):
    _attempt(lake, job, None)
    clock = Clock()
    with pytest.raises(chain.PreconditionFailed, match="latest attempt: running.*after waiting 90 min"):
        chain.require(lake, WEEK, job, wait_s=5400, sleep=clock.sleep, clock=clock)
    assert sum(clock.sleeps) == 5400 and max(clock.sleeps) == chain.POLL_S  # the whole wait, never longer


# --- what "done" means, and the bounds on the wait ----------------------------------------------------------------


def test_only_the_latest_attempt_counts(lake):
    """A done merge followed by one still running (a re-run, or one that died half-way) is not a finished merge;
    a dead first attempt followed by a done retry is."""
    t = datetime(2026, 11, 3, 3, 17, tzinfo=UTC)
    _attempt(lake, "merge", None, at=t)  # Cloud Run's first attempt, killed
    assert chain.state(lake, WEEK, "merge") == "running"
    _attempt(lake, "merge", "done", at=t + timedelta(minutes=20))  # its retry
    assert chain.state(lake, WEEK, "merge") == "done"
    _attempt(lake, "merge", None, at=t + timedelta(minutes=90))  # a re-run by hand, still going
    assert chain.state(lake, WEEK, "merge") == "running"
    assert chain.state(lake, WEEK, "enrich") == "missing"


def test_the_wait_is_capped_whatever_it_is_asked(lake):
    _attempt(lake, "merge", None)
    clock = Clock()
    with pytest.raises(chain.PreconditionFailed):
        chain.require(lake, WEEK, "merge", wait_s=10**9, sleep=clock.sleep, clock=clock)
    assert sum(clock.sleeps) == chain.MAX_WAIT_S < 240 * 60  # well inside the job's 240-minute timeout


def test_the_deadline_is_a_clock_time_shared_by_every_process_and_retry():
    at = lambda h, m: datetime(2026, 11, 3, h, m, tzinfo=UTC)  # noqa: E731
    assert (
        chain.wait_budget_s("07:47", at(6, 17)) == 90 * 60
    )  # the build job's slot: classify waits 90 minutes
    assert chain.wait_budget_s("07:47", at(7, 46)) == 60
    assert (
        chain.wait_budget_s("07:47", at(7, 47)) == 0
    )  # build, after classify waited: one check, no second wait
    assert chain.wait_budget_s("07:47", at(9, 0)) == 0  # Cloud Run's retry, or a run by hand later that day
    assert chain.wait_budget_s("07:47", at(1, 0)) == 0  # a run by hand long before: never a 6-hour wait
    assert chain.wait_budget_s(None, at(6, 17)) == chain.wait_budget_s("", at(6, 17)) == 0
    for bad in ("7.47", "soon", "25:00", "07:61"):
        with pytest.raises(ValueError, match="COFFEEDATA_AFTER_DEADLINE_UTC must be HH:MM"):
            chain.wait_budget_s(bad, at(6, 17))


def test_a_dropped_connection_while_waiting_is_polled_again_and_any_other_error_is_raised(lake, monkeypatch):
    _attempt(lake, "merge", "done")
    real, calls = chain.state, []

    def flaky(*a):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("Could not connect to server")
        return real(*a)

    monkeypatch.setattr(chain, "state", flaky)
    clock = Clock()
    assert chain.require(lake, WEEK, "merge", wait_s=600, sleep=clock.sleep, clock=clock)["status"] == "done"
    assert clock.sleeps == [chain.POLL_S]
    calls.clear()
    with pytest.raises(
        chain.PreconditionFailed, match="latest attempt: unreadable"
    ):  # no wait left: fail closed
        chain.require(lake, WEEK, "merge", wait_s=0, sleep=clock.sleep, clock=clock)
    monkeypatch.setattr(chain, "state", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        chain.require(lake, WEEK, "merge", wait_s=600, sleep=clock.sleep, clock=clock)


# --- the commands ---------------------------------------------------------------------------------------------------


class Reached(Exception):
    """The command got past its precondition and started its work."""


@pytest.fixture
def commands(lake, monkeypatch):
    """The three commands with their work replaced by `Reached`, as the Cloud Run jobs configure them, long after
    the deadline (so nothing waits). Returns run(command) -> None."""
    from coffeedata import quality, verdict

    def reached(*a, **k):
        raise Reached

    monkeypatch.setattr(connectors, "enrich_week", reached)
    monkeypatch.setattr(verdict, "require", lambda *a: True)
    monkeypatch.setattr(verdict, "collect", reached)
    monkeypatch.setattr(quality, "latest_run", reached)
    monkeypatch.setattr(cli, "_today", lambda: TUESDAY)
    monkeypatch.setattr(chain, "_utc_now", lambda: datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    monkeypatch.setenv(chain.DEADLINE_ENV, "07:47")
    monkeypatch.delenv(chain.SKIP_ENV, raising=False)

    def go(command: str, after: str | None) -> None:
        if after and command != "enrich-week":  # enrich-week needs the merge with or without the variable
            monkeypatch.setenv(chain.AFTER_JOB_ENV, after)
        else:
            monkeypatch.delenv(chain.AFTER_JOB_ENV, raising=False)
        args = ["--run-date", TUESDAY] if command == "enrich-week" else []
        cli.main([command, *args, *(["--db", str(lake.root / "x.duckdb")] if command == "build" else [])])

    return go


@pytest.mark.parametrize(("command", "after"), PRECONDITIONS)
def test_each_command_runs_after_a_done_predecessor(lake, commands, command, after):
    _attempt(lake, after, "done")
    with pytest.raises(Reached):
        commands(command, after)


@pytest.mark.parametrize(("command", "after"), PRECONDITIONS)
@pytest.mark.parametrize("status", ["failed", "missing", "running"])
def test_each_command_exits_non_zero_and_does_nothing_otherwise(lake, commands, command, after, status):
    """Non-zero, so the Cloud Run execution fails and the job-failure alert emails the owner (alerts.tf). The build
    job's shell keeps its `|| rc=1`, so a failed classify precondition is followed by a failed build one."""
    if status != "missing":
        _attempt(lake, after, None if status == "running" else status)
    before = control.rows(lake, WEEK)
    with pytest.raises(SystemExit) as e:
        commands(command, after)
    assert e.value.code not in (0, None)
    assert f"`{after}` is not done" in str(e.value.code) and f"latest attempt: {status}" in str(e.value.code)
    assert "RUNBOOK.md §5.2" in str(e.value.code) and chain.SKIP_ENV in str(e.value.code)
    assert control.rows(lake, WEEK) == before  # it wrote no row of its own: nothing started


@pytest.mark.parametrize("command", ["classify", "build"])
def test_build_and_classify_have_no_precondition_outside_the_weekly_job(lake, commands, command):
    """Locally, in CI and in weekly.yml nothing sets COFFEEDATA_AFTER_JOB: they run as before, with no merge row."""
    with pytest.raises(Reached):
        commands(command, None)


@pytest.mark.parametrize(("command", "after"), PRECONDITIONS)
def test_the_owner_can_override_a_precondition_and_it_is_logged(
    lake, commands, command, after, monkeypatch, caplog
):
    monkeypatch.setenv(chain.SKIP_ENV, "merge re-run by hand, enrich not needed")
    with caplog.at_level(logging.WARNING), pytest.raises(Reached):
        commands(command, after)
    assert "PRECONDITION SKIPPED by COFFEEDATA_SKIP_PRECONDITION" in caplog.text
    assert "merge re-run by hand, enrich not needed" in caplog.text
    assert f"{after}: latest attempt missing" in caplog.text  # what it skipped over


def test_an_overridden_enrich_week_runs_without_a_merge_and_says_so(lake, monkeypatch, capsys):
    from coffeedata import extract

    monkeypatch.setattr(extract, "llm_enabled", lambda: False)
    monkeypatch.setenv(chain.SKIP_ENV, "testing the override")
    cli.main(["enrich-week", "--run-date", TUESDAY])
    out = capsys.readouterr().out
    assert '"skipped": "testing the override"' in out and '"submitted": 0' in out
    assert [(r["job"], r["status"]) for r in control.rows(lake, WEEK)] == [("enrich", "done")]


def test_enrich_week_waits_for_a_merge_that_finishes_before_its_deadline(lake, monkeypatch, capsys):
    """The late start: the merge is still running at 04:17. enrich-week polls, and runs when the merge ends."""
    from coffeedata import extract

    monkeypatch.setattr(extract, "llm_enabled", lambda: False)
    monkeypatch.setenv(chain.DEADLINE_ENV, "05:47")
    monkeypatch.setattr(chain, "_utc_now", lambda: datetime(2026, 11, 3, 4, 17, tzinfo=UTC))
    row = _attempt(lake, "merge", None)
    slept = []

    def sleep(s: float) -> None:
        slept.append(s)
        if len(slept) == 2:
            control.finish(lake, row, "done")

    monkeypatch.setattr(chain.time, "sleep", sleep)
    cli.main(["enrich-week", "--run-date", TUESDAY])
    assert slept == [chain.POLL_S] * 2
    assert '"after": "merge"' in capsys.readouterr().out
    assert sorted((r["job"], r["status"]) for r in control.rows(lake, WEEK)) == [
        ("enrich", "done"),
        ("merge", "done"),
    ]


# --- the Cloud Run jobs (src/terraform/ingestion.tf; terraform itself is never run here) ----------------------------


def _tf() -> str:
    return (Path(__file__).parents[1] / "src/terraform/ingestion.tf").read_text()


def test_terraform_gives_the_two_later_jobs_their_predecessor_and_a_deadline_inside_their_timeout():
    tf = _tf()
    block = tf[tf.index("ingest_job_after = {") :]
    after = dict(
        (job, (pre, dl))
        for job, pre, dl in re.findall(
            r'"([a-z-]+)"\s+= \{ job = "([a-z-]+)", deadline_utc = "(\d\d:\d\d)" \}',
            block[: block.index("\n  }")],
        )
    )
    # the job_manifests `job` each waits for: the merge writes `merge`, enrich-week writes `enrich`
    assert {j: pre for j, (pre, _) in after.items()} == {"enrich-week": "merge", "build": "enrich"}
    cron = dict(
        re.findall(r'^\s+"([a-z-]+)"\s+= \{ job = "[a-z-]+", cron = "(\d+ \d+) \* \* 2" \}', tf, re.M)
    )

    def slot(job: str) -> timedelta:
        minute, hour = (int(x) for x in cron[job].split())
        return timedelta(hours=hour, minutes=minute)

    def deadline(job: str) -> timedelta:
        hh, mm = (int(x) for x in after[job][1].split(":"))
        return timedelta(hours=hh, minutes=mm)

    assert slot("merge") < slot("enrich-week") < slot("build")
    for job in after:
        wait = deadline(job) - slot(job)
        # a real wait, never over the cap, and at least 2 h of the 240-minute timeout left for the job's own work
        assert timedelta(0) < wait <= timedelta(seconds=chain.MAX_WAIT_S), (job, wait)
        assert timedelta(minutes=240) - wait >= timedelta(hours=2)
    assert deadline("enrich-week") <= slot("build")  # enrich-week has started (or failed) by the build's slot
    assert deadline("build") + timedelta(minutes=150) < timedelta(hours=11, minutes=17)  # before site.yml
    assert 'timeout         = "14400s"' in tf
    for name, field in ((chain.AFTER_JOB_ENV, "job"), (chain.DEADLINE_ENV, "deadline_utc")):
        assert re.search(
            r'dynamic "env" \{\s+for_each = \[for job, after in local\.ingest_job_after : after if job == each\.key\]'
            rf'\s+content \{{\s+name  = "{name}"\s+value = env\.value\.{field}\s+\}}',
            tf,
        ), name
    assert "length(setsubtract(keys(local.ingest_job_after), keys(local.ingest_jobs))) == 0" in tf
    assert chain.SKIP_ENV not in re.sub(r"#.*", "", tf)  # the override is never configured, only set by hand


def test_the_merge_is_never_retried_automatically():
    """max_retries = 0: Cloud Run never starts a merge over one that died part-way; `coffeedata merge` refuses
    that too (below), and the owner re-runs it by hand. enrich-week keeps its retry."""
    tf = _tf()
    assert re.search(r'"merge"\s+= \{ command = \["coffeedata"\], args = \["merge"\],[^}]*retries = 0 \}', tf)
    assert re.search(r'"enrich-week"\s+= \{[^}]*args = \["enrich-week", "--submit"\],[^}]*retries = 1 \}', tf)


# --- the merge, run twice ---------------------------------------------------------------------------------------------


class Killed(BaseException):
    """The merge's task dies here (an OOM kill, a crash): nothing after it runs, no end row is written."""


def _crawled_week(root: Path) -> Lake:
    """A week as Monday leaves it: two connectors' partitions (checkpoints, not yet compacted), both landed; a third
    connector (generic) never ran."""
    lk = Lake(str(root))
    run(lk, "2026-10-26", "s.com", [product("A", 20.0)])  # last week, already in the lake
    shopify = connectors.PartitionLake(lk, "shopify", WEEK)
    run(shopify, WEEK, "s.com", [product("A", 22.0), product("B")])
    run(shopify, WEEK, "s2.com", [product("C")])
    shopify.write_partition(
        "bodies",
        WEEK,
        "bb1",
        [{"body_hash": "bA", "body_text": "A", "first_seen_run": date.fromisoformat(WEEK)}],
    )
    woo = connectors.PartitionLake(lk, "woocommerce", WEEK)
    run(woo, WEEK, "w.com", None, adapter="woocommerce", hour=3)
    run(woo, WEEK, "w.com", [product("W")], adapter="woocommerce", hour=9)
    for c in ("shopify", "woocommerce"):
        control.finish(lk, control.start(lk, "crawl", WEEK, c), "done")
    return lk


def _lake_rows(lk: Lake) -> dict:
    """Every raw row the build reads, plus what is left to merge; run_manifests without the merge's own clock,
    and how many rows it holds (a repeat would show there)."""
    con = lk.connect()
    # batch_id is the test helper's counter, which differs between two lakes; a row stored twice still shows
    out = {
        t: sorted(con.execute(f"select * exclude (batch_id) from {lk.scan(t)}").fetchall(), key=repr)
        for t in ("fetches", "snapshots")
    }
    out["bodies"] = sorted(con.execute(f"select * from {lk.scan('bodies')}").fetchall(), key=repr)
    manifests = con.execute(
        f"select run_date, roaster_id, status, platform, adapter, mode from {lk.scan('run_manifests')} "
        "where roaster_id is not null"
    ).fetchall()
    out["expected"] = sorted(set(manifests), key=repr)
    out["unmerged"] = sorted(str(p.relative_to(lk.root)) for p in (lk.root / "raw").rglob("*.parquet"))
    return out, len(manifests)


def _die_writing_lake_table(n: int):
    """Dies as the merge writes its nth lake table (the compaction's own writes, under raw/, go through)."""
    real, seen = connectors._write_table, []

    def write(path, tbl):
        if "raw" not in path.parts:
            seen.append(path)
            if len(seen) == n:
                raise Killed
        real(path, tbl)

    return "_write_table", write


def _die_archiving(n: int, half: bool = False):
    """Dies as the merge archives its nth connector's partition; half: after GCSFuse's copy of the first file and
    before its delete (a rename there is a copy and a delete), so the file is in both places."""
    seen = []

    def archive(part, files, dest):
        seen.append(part)
        if len(seen) == n:
            if half:
                to = dest / files[0].relative_to(part)
                to.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(files[0], to)
            raise Killed

    return "_archive_files", archive


def _die_in(name: str):
    def die(*a, **k):
        raise Killed

    return name, die


STAGES = {
    "writing its first lake table": lambda: (connectors, *_die_writing_lake_table(1)),
    "between two lake tables": lambda: (connectors, *_die_writing_lake_table(3)),
    "before archiving anything": lambda: (connectors, *_die_archiving(1)),
    "half-way through a file's move to the archive": lambda: (connectors, *_die_archiving(1, half=True)),
    "between two connectors' archives": lambda: (connectors, *_die_archiving(2)),
    "before its end row": lambda: (control, *_die_in("finish")),
    "after its end row, before the rollup": lambda: (control, *_die_in("rollup")),
    "after the rollup, before the publish": lambda: (Lake, *_die_in("publish")),
}


@pytest.fixture
def registry_of_three(monkeypatch):
    states = [
        registry.RoasterState(Roaster(roaster_id=r, name=r, host=r, platform=p), "active", None)
        for r, p in (("s.com", "shopify"), ("s2.com", "shopify"), ("w.com", "woocommerce"), ("g.com", "wix"))
    ]
    monkeypatch.setattr(registry, "current", lambda lake, seed=None: states)


def _stored(lk: Lake, *folders: str) -> dict[str, str]:
    """path => sha256 of every file under these folders of the lake's root."""
    return {
        str(f.relative_to(lk.root)): hashlib.sha256(f.read_bytes()).hexdigest()
        for d in folders
        for f in sorted((lk.root / d).rglob("*"))
        if f.is_file()
    }


DATA = (
    "fetches",
    "snapshots",
    "bodies",
    "run_manifests",
    "archive",
    "raw",
)  # everything but the control table


def test_a_second_merge_of_a_merged_week_stores_nothing_and_changes_nothing(tmp_path, registry_of_three):
    """Ingestion only adds. Run twice (and a third time), the merge leaves every stored data file at the same path
    with the same bytes, and adds none: the raw tables, the archive, and no partition left behind. Only the control
    table grows, by each run's own start and end rows."""
    lk = _crawled_week(tmp_path / "lake")
    first = connectors.merge(lk, WEEK)
    assert first["written"] == {"run_manifests": 1, "fetches": 4, "snapshots": 4, "bodies": 1}
    before = _stored(lk, *DATA)
    assert len([p for p in before if p.startswith("archive/")]) == 5 and not any(
        p.startswith("raw/") for p in before
    )
    for _ in range(2):
        again = connectors.merge(lk, WEEK)
        assert again["written"] == {} and again["connectors"] == []
        assert _stored(lk, *DATA) == before  # same paths, same checksums, nothing new
    merges = [r for r in control.rows(lk, WEEK) if r["job"] == "merge"]
    assert [r["status"] for r in merges] == ["done"] * 3  # each run's own rows, none of another run's lost
    assert first["batch"] in {control.detail_of(r)["batch"] for r in merges}


def test_a_later_merge_for_a_late_connector_only_adds_files(tmp_path, registry_of_three):
    """The one re-run with something to do: a connector that landed after the first merge. Every file the first
    merge stored keeps its path and bytes; the second adds its own."""
    lk = _crawled_week(tmp_path / "lake")
    connectors.merge(lk, WEEK)
    before = _stored(lk, *DATA)
    run(connectors.PartitionLake(lk, "generic", WEEK), WEEK, "g.com", [product("G")], adapter="generic")
    control.finish(lk, control.start(lk, "crawl", WEEK, "generic"), "done")
    out = connectors.merge(lk, WEEK)
    assert out["written"] == {"fetches": 1, "snapshots": 1} and out["connectors"] == ["generic"]
    after = _stored(lk, *DATA)
    assert before.items() <= after.items()
    assert len(after) - len(before) == 4  # a fetches and a snapshots file in the lake, and the two archived


@pytest.mark.parametrize("stage", STAGES)
def test_a_merge_run_again_after_one_that_died_loses_and_repeats_nothing(
    tmp_path, monkeypatch, registry_of_three, stage
):
    """What the owner's re-run does after a merge died (Cloud Run itself never retries it). Whatever step the
    first attempt died at, the lake's rows end up exactly as one clean merge leaves them: no row twice, none lost,
    nothing left unmerged. And the re-run only adds: every lake file the dead attempt stored keeps its path and
    bytes. What does differ from a clean merge is where the archive is: each run moves what it finds to its own
    `merge=<batch>` folder, so the partition's archive is split across two (same bytes, nothing dropped)."""
    clean = _crawled_week(tmp_path / "clean")
    assert connectors.merge(clean, WEEK)["missing"] == ["generic", "square_online", "squarespace"]
    want, want_manifests = _lake_rows(clean)
    assert len(want["fetches"]) == 5 and len(want["snapshots"]) == 5 and want["unmerged"] == []
    assert [r[1] for r in want["expected"]] == ["g.com"] and want_manifests == 1

    lk = _crawled_week(tmp_path / "retried")
    owner, name, die = STAGES[stage]()
    with monkeypatch.context() as m:
        m.setattr(owner, name, die)
        m.setattr(control, "now", lambda: datetime(2026, 11, 3, 3, 17, tzinfo=UTC))  # the scheduled slot
        with pytest.raises(Killed):
            connectors.merge(lk, WEEK)
    if "end row" not in stage and "rollup" not in stage and "publish" not in stage:
        assert chain.state(lk, WEEK, "merge") == "running"  # the dead attempt: enrich-week and the build wait
    stored = _stored(lk, "fetches", "snapshots", "bodies", "run_manifests", "archive")
    connectors.merge(lk, WEEK)  # the owner's re-run
    got, got_manifests = _lake_rows(lk)
    assert got == want and got_manifests == want_manifests
    assert stored.items() <= _stored(lk, "fetches", "snapshots", "bodies", "run_manifests", "archive").items()
    assert chain.state(lk, WEEK, "merge") == "done"
    final = _stored(lk, *DATA)
    connectors.merge(lk, WEEK)  # and a third time
    assert _lake_rows(lk)[0] == want and _stored(lk, *DATA) == final


def test_merge_refuses_to_start_over_an_attempt_that_never_ended(lake, monkeypatch, caplog):
    """A start row with no end row is a merge still running, or one that died part-way. The command exits non-zero
    without touching anything; the owner's override, once they have checked nothing is running, is logged."""
    monkeypatch.setattr(connectors, "merge", lambda *a: {"ran": True})
    monkeypatch.delenv(chain.SKIP_ENV, raising=False)
    cli.main(["merge", "--run-date", TUESDAY])  # no earlier attempt: runs
    _attempt(lake, "merge", "done")
    cli.main(["merge", "--run-date", TUESDAY])  # after a finished one (a late connector): runs
    _attempt(lake, "merge", None)
    before = (_stored(lake, "job_manifests"), control.rows(lake, WEEK))
    with pytest.raises(SystemExit) as e:
        cli.main(["merge", "--run-date", TUESDAY])
    assert "earlier `merge` attempt of this week (2026-11-02) has a start row and no end row" in str(
        e.value.code
    )
    assert (
        _stored(lake, "job_manifests"),
        control.rows(lake, WEEK),
    ) == before  # it only read the control table
    monkeypatch.setenv(chain.SKIP_ENV, "the 03:17 execution was OOM-killed, nothing is running")
    with caplog.at_level(logging.WARNING):
        cli.main(["merge", "--run-date", TUESDAY])
    assert "PRECONDITION SKIPPED" in caplog.text and "OOM-killed" in caplog.text


def test_the_preconditions_only_read_the_control_table(lake, commands):
    """Passing, failing or waiting, a precondition writes, edits and deletes nothing in job_manifests."""
    _attempt(lake, "merge", "done")
    _attempt(lake, "enrich", None)
    before = _stored(lake, "job_manifests")
    clock = Clock()
    chain.require(lake, WEEK, "merge", wait_s=600, sleep=clock.sleep, clock=clock)
    with pytest.raises(chain.PreconditionFailed):
        chain.require(lake, WEEK, "enrich", wait_s=600, sleep=clock.sleep, clock=clock)
    for command in ("classify", "build"):
        with pytest.raises(SystemExit):
            commands(command, "enrich")
    assert _stored(lake, "job_manifests") == before and len(before) >= 3


@needs_dbt
def test_a_week_merged_twice_builds_the_same_marts_as_one_merged_once(
    tmp_path, monkeypatch, registry_of_three
):
    """dbt on the same raw gives the same output: a merge that died and was run again, then run once more, hands
    the build what one clean merge does."""
    from .test_connectors import _marts
    from .test_dbt import build

    clean = _crawled_week(tmp_path / "clean")
    connectors.merge(clean, WEEK)
    lk = _crawled_week(tmp_path / "retried")
    with monkeypatch.context() as m:
        m.setattr(control, "finish", _die_in("finish")[1])
        with pytest.raises(Killed):
            connectors.merge(lk, WEEK)
    connectors.merge(lk, WEEK)
    connectors.merge(lk, WEEK)
    a, b = _marts(build(clean, tmp_path / "a")), _marts(build(lk, tmp_path / "b"))
    assert a == b and a["events"] and len(a["complete"]) == 4
