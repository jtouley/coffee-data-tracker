"""The Tuesday chain's precondition (issue #84): a job starts its work only once the jobs before it have finished.

merge -> enrich-week -> build still start on Cloud Scheduler's fixed times (src/terraform/ingestion.tf). Before it
does anything, a job reads the control table (job_manifests) and asks, for each job before it: is its latest attempt
for this week `done`, and has it published what it wrote? If so it goes on. If not, it waits, polling, until a
deadline, and then fails with a non-zero exit, so the execution fails and the job-failure alert emails the owner
(alerts.tf). Nothing runs on a week that is missing, half-merged or still being written.

What a job is told, by environment variables set on its Cloud Run job:

- COFFEEDATA_AFTER_JOB: the job_manifests `job`s that must be done, comma-separated (`merge` for enrich-week,
  `merge,enrich` for the build job's classify and build). Unset: no precondition, which is how `coffeedata build`
  and `classify` run everywhere else (locally, in CI, in the old weekly workflow). enrich-week has always needed
  the merge and still does.
- COFFEEDATA_AFTER_DEADLINE_UTC: `HH:MM`, today, UTC: how long to wait for them. A clock time, not a duration, so
  every process of an execution (classify, then build) and Cloud Run's retry of it (enrich-week has one) share one
  deadline instead of each waiting afresh. Past it, or more than MAX_WAIT_S before it (a run by hand at another
  hour), the check is made once.
- COFFEEDATA_SKIP_PRECONDITION: a reason. The owner's override for a run by hand: the check is skipped, and the
  reason and the state it skipped over are logged. A false-like word (0, false, no, off) is not a reason: the check
  stays on.

Only `done` passes. A `running` row is either a job at work or one that died (a killed attempt never writes an end
row), and a `failed` one may be followed by a retry, so neither ends the wait early: the deadline does. And `done`
passes only once the job's publish has cleared its end row from staging: the end row is written before the publish,
and until the publish commits, the week's files are still on their way to the dataset.

While waiting, a read that fails because the job being waited for is at that moment replacing the files (the
merge's rollup and every publish remove the week's job_manifests files from staging), or on the connection, or on a
429, counts as "not done yet" and is tried again at the next poll, until the same deadline. Any other failure (a
permission or schema error, a corrupt file, a bug) is raised at once: waiting would not fix it.

The merge has no job before it, but it has a guard of the same kind (`refuse_unfinished`): it won't start over an
earlier merge attempt of the week that failed or never ended, since that attempt may have stopped part-way.

Everything here only reads job_manifests. The one thing it writes is a marker file in the container's own temp
folder (`mark_failed`), so that in the build job a classify whose precondition did not pass stops the build too.
"""

from __future__ import annotations

import errno
import logging
import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from .lake import Lake, no_files, transient

log = logging.getLogger(__name__)

AFTER_JOB_ENV = "COFFEEDATA_AFTER_JOB"
DEADLINE_ENV = "COFFEEDATA_AFTER_DEADLINE_UTC"
SKIP_ENV = "COFFEEDATA_SKIP_PRECONDITION"

POLL_S = 60.0  # between reads of the control table while waiting
MAX_WAIT_S = 120 * 60.0  # no wait is ever longer, whatever the deadline says: half a job's 240-minute timeout
# A check with no wait left (a run by hand, a retry past the deadline) still gets these short retries of a read
# that failed, in seconds, before it fails closed.
UNREADABLE_WAITS = (2.0, 6.0, 18.0)
NOT_A_REASON = {"0", "false", "no", "off", "n", "f", "none", "null"}
# What a read looks like, in DuckDB's or the filesystem's error text, when a file listed a moment ago is gone or was
# replaced (GCSFuse: a stale handle), or the dataset's host answered 429. lake.transient covers the connection.
VANISHED_MARKERS = ("no such file", "stale file handle", "http 429", "too many requests")
# ... and what is never worth waiting through, even where a transient marker also matches ("connection already
# closed" contains "connection"): a refused permission or credential, a closed connection object (a bug), a schema.
FATAL_MARKERS = (
    "permission denied",
    "forbidden",
    "unauthorized",
    "http 401",
    "http 403",
    "connection already closed",
    "binder error",
)
NOT_PUBLISHED = "done, not yet published"


class PreconditionFailed(Exception):
    """A job before this one isn't done for the week, and the wait (if any) is over."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def retryable(e: Exception) -> bool:
    """A read of the control table that is worth trying again: a dropped connection (lake.transient), a file that
    vanished or went stale between the listing and the read, or a 429. Never a permission, credential or schema
    error, or a closed connection object, whatever else the message says."""
    msg = str(e).lower()
    if isinstance(e, PermissionError) or any(m in msg for m in FATAL_MARKERS):
        return False
    if isinstance(e, FileNotFoundError) or (isinstance(e, OSError) and e.errno == errno.ESTALE):
        return True
    return transient(e) or any(m in msg for m in VANISHED_MARKERS)


def _week_folder(lake: Lake, week: str) -> Path:
    return lake.root / "job_manifests" / f"run_date={week}"


def week_rows(lake: Lake, week: str) -> list[dict]:
    """The week's job_manifests rows, one per attempt (its end row if it has one), read from that week's folder
    only, staged and remote: a poll never lists the whole table. A folder with no files reads as no rows."""
    sources = [str(_week_folder(lake, week) / "*.parquet")]
    if lake.remote and lake.remote_has("job_manifests"):
        sources.append(f"{lake.url}/job_manifests/run_date={week}/*.parquet")
    con = lake.connect()
    best: dict[str, dict] = {}
    for src in sources:
        try:
            cur = con.execute(
                "select attempt_id, job, status, started_at, ended_at, detail "
                f"from read_parquet('{src}', union_by_name = true, hive_partitioning = false)"
            )
            cols = [d[0] for d in cur.description]
            found = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        except Exception as e:
            if no_files(e):
                continue
            raise
        for r in found:
            if not r["attempt_id"]:
                continue
            # an end row over a start row; then the later end; on a tie to the second, anything but `done` (a
            # `failed` written in the same second as a `done` must not read as done: fail closed)
            ended = r["ended_at"].timestamp() if r["ended_at"] else 0.0
            rank = (r["status"] != "running", ended, r["status"] != "done")
            if r["attempt_id"] not in best or rank > best[r["attempt_id"]]["_rank"]:
                best[r["attempt_id"]] = r | {"_rank": rank}
    return [{k: v for k, v in r.items() if k != "_rank"} for r in best.values()]


def latest(lake: Lake, week: str, job: str, rows: list[dict] | None = None) -> dict | None:
    """The latest attempt (by start time) of `job` for the week, or None. The latest, not any: a done merge
    followed by one still running (a re-run for a late connector, or one that died half-way) is not a finished
    merge. started_at is to the second: of two attempts in the same one, the unfinished one counts (fail closed).
    `rows`: the week's rows already read (week_rows), so one poll reads the week once whatever it checks."""
    attempts = [r for r in (week_rows(lake, week) if rows is None else rows) if r["job"] == job]
    return max(attempts, key=lambda r: (r["started_at"], r["status"] != "done")) if attempts else None


def _still_staged(lake: Lake, week: str, attempt_id: str) -> bool:
    """Whether the attempt's `done` row is still in staging of a remote lake: its job hasn't published yet (a
    publish uploads everything staged in one commit, then clears staging). A local lake has no publish to wait
    for. A file that vanishes while it is read means staging is changing under this read: the merge's rollup
    replaces the week's files with one (still staged) before its publish, so a vanished file proves nothing. It
    counts as still staged, and the next poll decides."""
    if not lake.remote:
        return False
    for f in sorted(_week_folder(lake, week).glob("*.parquet")):
        try:
            rows = pq.read_table(f, columns=["attempt_id", "status"]).to_pylist()
        except Exception as e:
            if retryable(e):
                log.warning(
                    "a staged job_manifests file changed while it was read (%s): not published yet", e
                )
                return True
            raise
        if any(r["attempt_id"] == attempt_id and r["status"] == "done" for r in rows):
            return True
    return False


def state(lake: Lake, week: str, job: str, rows: list[dict] | None = None) -> str:
    """The status of the latest attempt of `job` for the week: done, running, failed or deadline; `missing` when it
    has no row; NOT_PUBLISHED when its end row says done and its publish hasn't cleared staging yet."""
    row = latest(lake, week, job, rows)
    if row is None:
        return "missing"
    if row["status"] == "done" and _still_staged(lake, week, row["attempt_id"]):
        return NOT_PUBLISHED
    return row["status"]


def wait_budget_s(deadline: str | None, now: datetime) -> float:
    """Seconds to wait for the jobs before this one: until `deadline` (HH:MM, today, UTC), and 0 with no deadline,
    past it, or more than MAX_WAIT_S before it. A deadline that isn't HH:MM is a ValueError: a misconfigured job
    fails loudly instead of quietly not waiting."""
    if not deadline:
        return 0.0
    try:
        hh, mm = deadline.strip().split(":")
        at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except ValueError:
        raise ValueError(f"{DEADLINE_ENV} must be HH:MM (UTC), not {deadline!r}") from None
    left = (at - now).total_seconds()
    if left > MAX_WAIT_S:
        log.warning(
            "the deadline %s UTC is more than %d min away: not this job's scheduled slot, so no waiting",
            deadline,
            MAX_WAIT_S // 60,
        )
        return 0.0
    return max(left, 0.0)


def _states(lake: Lake, week: str, jobs: tuple[str, ...]) -> tuple[dict[str, str], str | None]:
    """Each job's state, from one read of the week; `unreadable` for all, with the error, when the read fails in a
    way worth trying again (retryable). Any other error is raised, at once."""
    try:
        rows = week_rows(lake, week)
        return {job: state(lake, week, job, rows) for job in jobs}, None
    except Exception as e:
        if not retryable(e):
            raise
        log.warning("reading job_manifests failed (%s)", e)
        return dict.fromkeys(jobs, "unreadable"), f"{type(e).__name__}: {e}"[:300]


def _describe(states: dict[str, str]) -> str:
    return ", ".join(f"`{job}`: {status}" for job, status in states.items())


def require(
    lake: Lake,
    week: str,
    jobs: str | tuple[str, ...],
    wait_s: float = 0.0,
    *,
    poll_s: float = POLL_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict:
    """Return once the latest attempt of every one of `jobs` for the week is done and published, polling for at
    most `wait_s` seconds (capped at MAX_WAIT_S); raise PreconditionFailed if one still isn't. A read that fails
    in a retryable way counts as "not done yet" and is tried again at the next poll, until the same deadline; with
    no wait left it gets UNREADABLE_WAITS, then fails closed, naming the last read error. Any other error (a
    permission or schema error) is raised at once."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    jobs = (jobs,) if isinstance(jobs, str) else tuple(jobs)
    budget = min(max(wait_s, 0.0), MAX_WAIT_S)
    started = clock()
    grace = list(UNREADABLE_WAITS)
    last_error = None
    while True:
        states, error = _states(lake, week, jobs)
        last_error = error or last_error
        waited = clock() - started
        if all(s == "done" for s in states.values()):
            if waited >= 1:
                log.warning("%s for %s: done after a wait of %.0f min", ", ".join(jobs), week, waited / 60)
            return {"after": list(jobs), "status": "done", "waited_s": round(waited)}
        left = budget - waited
        if left <= 0:
            if "unreadable" in states.values() and grace:
                sleep(grace.pop(0))
                continue
            raise PreconditionFailed(
                f"precondition failed: this week's ({week}) jobs before this one are not all done in job_manifests "
                f"(latest attempts: {_describe(states)}), after waiting {waited / 60:.0f} min"
                + (
                    f"; the last failed read: {last_error}"
                    if "unreadable" in states.values() and last_error
                    else ""
                )
                + ". Nothing was run. "
                f"See docs/ingestion/RUNBOOK.md §5.2; to run anyway, set {SKIP_ENV}=<reason> on the execution."
            )
        log.warning(
            "waiting for this week's (%s) %s; %.0f min left",
            week,
            _describe(states).replace("`", ""),
            left / 60,
        )
        sleep(min(poll_s, left))


def skip_reason() -> str | None:
    """The owner's override, if COFFEEDATA_SKIP_PRECONDITION holds a reason. A false-like word is not one: someone
    writing `=false` to turn the override off must not turn the precondition off."""
    value = os.environ.get(SKIP_ENV, "").strip()
    if not value:
        return None
    if value.lower() in NOT_A_REASON:
        log.warning("%s=%r is not a reason: the precondition stays on", SKIP_ENV, value)
        return None
    return value


def check(lake: Lake, day: str, default_jobs: str | None = None) -> dict | None:
    """The precondition of the job this process belongs to, from its environment (see the module's docstring):
    None when it has none. `day` is any day of the week it runs for. Raises PreconditionFailed."""
    from .connectors import week_of

    jobs = tuple(
        j.strip() for j in (os.environ.get(AFTER_JOB_ENV) or default_jobs or "").split(",") if j.strip()
    )
    if not jobs:
        return None
    week = week_of(day)
    if reason := skip_reason():
        try:
            rows = week_rows(lake, week)
            states = {job: state(lake, week, job, rows) for job in jobs}
        except Exception as e:  # the override must work even when the control table can't be read
            states = dict.fromkeys(jobs, f"unreadable ({type(e).__name__})")
        log.warning(
            "PRECONDITION SKIPPED by %s: %r. This week's (%s) latest attempts: %s",
            SKIP_ENV,
            reason,
            week,
            _describe(states).replace("`", ""),
        )
        return {"after": list(jobs), "status": states, "skipped": reason}
    return require(lake, week, jobs, wait_budget_s(os.environ.get(DEADLINE_ENV), _utc_now()))


def _latest_with_retries(lake: Lake, week: str, job: str, sleep: Callable[[float], None]) -> dict | None:
    """latest(), with UNREADABLE_WAITS between tries of a read that fails in a retryable way; then it fails closed
    (PreconditionFailed, naming the error). Any other error is raised at once."""
    grace = list(UNREADABLE_WAITS)
    while True:
        try:
            return latest(lake, week, job)
        except Exception as e:
            if not retryable(e):
                raise
            if not grace:
                raise PreconditionFailed(
                    f"precondition failed: this week's ({week}) job_manifests could not be read to check for an "
                    f"unfinished `{job}` ({type(e).__name__}: {str(e)[:300]}). Nothing was run. See "
                    f"docs/ingestion/RUNBOOK.md §5.2; to run anyway, set {SKIP_ENV}=<reason> on the execution."
                ) from None
            log.warning("reading job_manifests failed (%s); trying again", e)
            sleep(grace.pop(0))


def refuse_unfinished(
    lake: Lake, day: str, job: str, *, sleep: Callable[[float], None] | None = None
) -> dict | None:
    """Refuse to start `job` over its latest attempt this week if that one failed or has no end row. No end row:
    it is still running (two merges at once would each add the other's rows) or was killed part-way. Failed: it
    raised, perhaps part-way. Redoing its work is the owner's decision, not a blind retry's. Raises
    PreconditionFailed; COFFEEDATA_SKIP_PRECONDITION=<reason> is the owner's logged go-ahead, once they have
    checked that nothing is running, and it is honoured before the control table is read, so it works when the
    table can't be."""
    from .connectors import week_of
    from .control import detail_of

    week = week_of(day)
    if reason := skip_reason():
        try:
            row = latest(lake, week, job)
            status = row["status"] if row else "missing"
        except Exception as e:
            status = f"unreadable ({type(e).__name__})"
        log.warning(
            "PRECONDITION SKIPPED by %s: %r. This week's (%s) latest %s attempt: %s",
            SKIP_ENV,
            reason,
            week,
            job,
            status,
        )
        return {"unfinished": job, "status": status, "skipped": reason}
    row = _latest_with_retries(lake, week, job, sleep or time.sleep)
    if row is None or row["status"] not in ("running", "failed"):
        return None
    if row["status"] == "failed":
        what = f"failed ({detail_of(row).get('error') or 'no error recorded'}); it may have stopped part-way"
    else:
        what = "has a start row and no end row: it is still running, or it was killed part-way"
    raise PreconditionFailed(
        f"precondition failed: the latest `{job}` attempt of this week ({week}), started {row['started_at']}, "
        f"{what}. Nothing was run. Check that no {job} execution is running (docs/ingestion/RUNBOOK.md §5.2), then "
        f"run it again with {SKIP_ENV}=<reason>."
    )


# --- one execution, several commands: the build job runs classify, then build ---------------------------------------


def _marker(week: str) -> Path:
    """A file in this container's own temp folder (never the lake or staging), named for the Cloud Run execution
    and task attempt, so another execution, or the next week, never sees it."""
    run = f"{os.environ.get('CLOUD_RUN_EXECUTION', 'local')}-{os.environ.get('CLOUD_RUN_TASK_ATTEMPT', '0')}"
    return Path(tempfile.gettempdir()) / f"coffeedata-precondition-failed-{run}-{week}"


def mark_failed(day: str, why: str) -> None:
    """Record that a command's precondition did not pass in this execution (it failed, or raised)."""
    from .connectors import week_of

    if not os.environ.get(AFTER_JOB_ENV):  # only in the weekly jobs: a local run leaves nothing behind
        return
    try:
        _marker(week_of(day)).write_text(why[:500])
    except OSError as e:  # the command still exits non-zero; only the hand-over to the next command is lost
        log.warning("could not record the failed precondition for the next command: %s", e)


def refuse_after_failed(day: str) -> None:
    """Raise PreconditionFailed if an earlier command of this execution did not pass its precondition. The build
    job runs `classify` and then `build` whatever classify's exit: without this, a classify that failed its check
    (or died in it) could be followed by a build that passes a moment later, on last week's verdicts."""
    from .connectors import week_of

    marker = _marker(week_of(day))
    if not os.environ.get(AFTER_JOB_ENV) or not marker.exists() or skip_reason():
        return
    raise PreconditionFailed(
        "precondition failed: an earlier command of this execution did not pass its precondition "
        f"({marker.read_text()[:300]}). Nothing was run."
    )
