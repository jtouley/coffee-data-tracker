"""The Tuesday chain's precondition (issue #84): a job starts its work only once the job before it has finished.

merge -> enrich-week -> build still start on Cloud Scheduler's fixed times (src/terraform/ingestion.tf). Before it
does anything, a job reads the control table (job_manifests) and asks: is the latest attempt of the job before me,
for this week, `done`? If so it goes on. If not, it waits, polling, until a deadline, and then fails with a non-zero
exit, so the execution fails and the job-failure alert emails the owner (alerts.tf). Nothing runs on a week that is
missing, half-merged or still being written.

What a job is told, by environment variables set on its Cloud Run job:

- COFFEEDATA_AFTER_JOB: the job_manifests `job` that must be done (`merge` for enrich-week, `enrich` for the build
  job's classify and build). Unset: no precondition, which is how `coffeedata build` and `classify` run everywhere
  else (locally, in CI, in the old weekly workflow). enrich-week has always needed the merge and still does.
- COFFEEDATA_AFTER_DEADLINE_UTC: `HH:MM`, today, UTC: how long to wait for it. A clock time, not a duration, so
  every process of an execution (classify, then build) and Cloud Run's retry of it share one deadline instead of
  each waiting afresh. Past it, or more than MAX_WAIT_S before it (a run by hand at another hour), the check is
  made once.
- COFFEEDATA_SKIP_PRECONDITION: a reason. The owner's override for a run by hand: the check is skipped, and the
  reason and the state it skipped over are logged.

The merge has no job before it, but it has a guard of the same kind (`refuse_unfinished`): it won't start while an
earlier merge attempt of the week has no end row, since that attempt is either still writing or died part-way.

Everything here only reads job_manifests. Only `done` passes. A `running` row is either a job at work or one that died (a killed attempt never writes an end
row), and a `failed` one may be followed by Cloud Run's retry, so neither ends the wait early: the deadline does.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

from . import control
from .lake import Lake, retry_transient, transient

log = logging.getLogger(__name__)

AFTER_JOB_ENV = "COFFEEDATA_AFTER_JOB"
DEADLINE_ENV = "COFFEEDATA_AFTER_DEADLINE_UTC"
SKIP_ENV = "COFFEEDATA_SKIP_PRECONDITION"

POLL_S = 60.0  # between reads of the control table while waiting
MAX_WAIT_S = 120 * 60.0  # no wait is ever longer, whatever the deadline says: half a job's 240-minute timeout


class PreconditionFailed(Exception):
    """The job before this one isn't done for the week, and the wait (if any) is over."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def state(lake: Lake, week: str, job: str) -> str:
    """The status of the latest attempt (by start time) of `job` for the week: done, running, failed or deadline;
    `missing` when it has no row. The latest, not any: a done merge followed by one still running (a re-run for a
    late connector, or one that died half-way) is not a finished merge."""
    attempts = [r for r in retry_transient(control.rows, lake, week) if r["job"] == job]
    if not attempts:
        return "missing"
    # started_at is to the second: of two attempts in the same one, the unfinished one counts (fail closed)
    return max(attempts, key=lambda r: (r["started_at"], r["status"] != "done"))["status"]


def wait_budget_s(deadline: str | None, now: datetime) -> float:
    """Seconds to wait for the job before this one: until `deadline` (HH:MM, today, UTC), and 0 with no deadline,
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
            deadline, MAX_WAIT_S // 60,
        )  # fmt: skip
        return 0.0
    return max(left, 0.0)


def require(
    lake: Lake,
    week: str,
    job: str,
    wait_s: float = 0.0,
    *,
    poll_s: float = POLL_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict:
    """Return once the latest attempt of `job` for the week is done, polling for at most `wait_s` seconds (capped
    at MAX_WAIT_S); raise PreconditionFailed if it still isn't. A read of the control table that fails on the
    connection counts as "not done yet" and is tried again at the next poll; any other error is raised."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    budget = min(max(wait_s, 0.0), MAX_WAIT_S)
    started = clock()
    while True:
        try:
            status = state(lake, week, job)
        except Exception as e:
            if not transient(e):
                raise
            log.warning("reading job_manifests failed (%s)", e)
            status = "unreadable"
        waited = clock() - started
        if status == "done":
            if waited:
                log.warning("%s for %s is done after a wait of %.0f min", job, week, waited / 60)
            return {"after": job, "status": status, "waited_s": round(waited)}
        left = budget - waited
        if left <= 0:
            raise PreconditionFailed(
                f"precondition failed: this week's ({week}) `{job}` is not done in job_manifests (its latest "
                f"attempt: {status}), after waiting {waited / 60:.0f} min. Nothing was run. See "
                f"docs/ingestion/RUNBOOK.md §5.2; to run anyway, set {SKIP_ENV}=<reason> on the execution."
            )
        log.warning(
            "waiting for this week's (%s) %s: its latest attempt is %s; %.0f min left",
            week,
            job,
            status,
            left / 60,
        )
        sleep(min(poll_s, left))


def check(lake: Lake, day: str, default_job: str | None = None) -> dict | None:
    """The precondition of the job this process belongs to, from its environment (see the module's docstring):
    None when it has none. `day` is any day of the week it runs for. Raises PreconditionFailed."""
    from .connectors import week_of

    job = os.environ.get(AFTER_JOB_ENV, "").strip() or default_job
    if not job:
        return None
    week = week_of(day)
    if reason := os.environ.get(SKIP_ENV, "").strip():
        try:
            status = state(lake, week, job)
        except Exception as e:  # the override must work even when the control table can't be read
            status = f"unreadable ({type(e).__name__})"
        log.warning(
            "PRECONDITION SKIPPED by %s: %r. This week's (%s) %s: latest attempt %s",
            SKIP_ENV, reason, week, job, status,
        )  # fmt: skip
        return {"after": job, "status": status, "skipped": reason}
    return require(lake, week, job, wait_budget_s(os.environ.get(DEADLINE_ENV), _utc_now()))


def refuse_unfinished(lake: Lake, day: str, job: str) -> dict | None:
    """Refuse to start `job` while its latest attempt this week has a start row and no end row: that attempt is
    either still running (two merges at once would each add the other's rows) or died part-way, and redoing its
    work is the owner's decision, not a blind retry's. Raises PreconditionFailed; COFFEEDATA_SKIP_PRECONDITION
    =<reason> is the owner's logged go-ahead, once they have checked that nothing is running."""
    from .connectors import week_of

    week = week_of(day)
    if state(lake, week, job) != "running":
        return None
    if reason := os.environ.get(SKIP_ENV, "").strip():
        log.warning(
            "PRECONDITION SKIPPED by %s: %r. This week's (%s) earlier %s attempt has no end row",
            SKIP_ENV, reason, week, job,
        )  # fmt: skip
        return {"unfinished": job, "skipped": reason}
    raise PreconditionFailed(
        f"precondition failed: an earlier `{job}` attempt of this week ({week}) has a start row and no end row in "
        f"job_manifests: it is still running, or it died part-way. Nothing was run. Check that no {job} execution "
        f"is running (docs/ingestion/RUNBOOK.md §5.2), then run it again with {SKIP_ENV}=<reason>."
    )
