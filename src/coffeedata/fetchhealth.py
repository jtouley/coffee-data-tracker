"""The fetch-health check (`coffeedata fetch-health`): did each roaster's fetch this week return a catalog of about
the right size? Run on the week's crawl output shortly after each crawl pass ends, before the merge, it lists the
roasters whose fetch failed, came back empty or came back far smaller than usual, and finds a systemic failure on
Monday instead of at Tuesday's build. It judges fetches, not data: it never looks at which products came back or
what they say (prices, weights, the products themselves), so it can't say the data is good, and doesn't claim to
(the dq-audit of 2026-11-02 found wiped prices, swapped products and short first weeks that it passes).

What it runs is the gate's own rules, not a copy of them: dbt builds run_completeness and stg_run_quality (and
what they read) into a scratch database and runs three of the gate's singular tests with their failing rows stored:
- run_ok_rate and adapter_ok_rate, with var attempted_only (the roasters a crawl pass has reached: a roaster a pass
  left for its resume slot is not a failure yet), the same thresholds as the build;
- suspect_runs_need_review: stg_run_quality's suspect rule (fewer than half the median of the last 4 counted weeks).
dbt reads the lake plus a copy of the connector partitions (raw/connector=*/run_date=<week>/, which dbt's globs never
see) laid out as one more lake root. Only fetches and run_manifests are copied: every column judged here comes from
them (and earlier weeks); snapshots and bodies are what the build classifies.

The list (`repull`): per roaster, the rows a named gate rule counts as not ok, less partial fetches, and nothing
else (RULES): `failed` (no ok fetch) and `empty` (an ok fetch with no products, for a roaster known to have a
catalog), the rows run_ok_rate counts against the run (run_completeness has_catalog, attempted, not catalog_ok) less
the partial ones (below); `suspect`, a failing row of suspect_runs_need_review for the checked week. Each entry
records its rules by name (`rules`). Anything the rules can't tell is left alone (CLAUDE.md: abstain).

Main's fetch states, each mapped on purpose (run_completeness carries them):
- partial (an ok fetch that read only part of the catalog: stopped by the deadline, a page block, a total it
  couldn't reach; fetches.partial, its reason in fetches.error): not failed. The gate counts it as ok when it has
  products, and stg_run_walk never makes it suspect or puts it in a baseline. So it is never a reason on the list,
  not even with 0 products (which run_ok_rate counts as not ok): it is listed apart (`partial`, with its reason),
  never re-fetched by this check (a partial fetch isn't done, so the week's own resume passes read it again), never
  held back, never systemic.
- crashed (quarantined by crashguard after crashing two attempts of its crawl): one store's problem with our
  crawler, which the gate leaves out of both ok rates (not has_catalog). Listed apart (`quarantined`), never
  re-fetched (it is quarantined for the week), never systemic.
- a challenge (an edge's anti-bot page instead of the catalog, a 202 among them): an ordinary failed fetch, which
  the week's later passes retry. `failed` on the list, never held back, and the entry names the edge when a
  recorded block names it (`challenge`: SiteGround's sg-captcha, Cloudflare's cf-mitigated; stg_fetches.challenge).
  It has no systemic scope of its own: a platform it fails as a whole is adapter_ok_rate's `connector` finding, like
  any failure. Persistent blocking is a separate follow-up (#110).
- a refusal that outlasts the 5-minute wait cap (http.MAX_STORE_WAIT: a 429, 430 or 503, or a 5xx's long
  Retry-After): an error fetch, so `failed`, counted by run_ok_rate and adapter_ok_rate as any failure (a platform
  rate-limiting most stores is scope `connector`). Every later crawl pass of the week asks it again anyway.

What it decides, with no threshold of its own:
- systemic: a platform below adapter_ok_rate's floor (scope connector); two or more platforms below it in the same
  pass, which points at what they share, the image, our code or its egress, rather than at one platform (scope
  image); the run below run_ok_rate's floor with no single platform to blame (scope run). The job exits non-zero,
  which emails the owner (alerts.tf, ingest_job_failed), once per finding a week: a later check that finds the same
  scope on the same connectors again records it and logs a warning; a new or different finding alerts.
- held back: a systemic platform's roasters stay off the list (asking a platform that is refusing us again the same
  evening helps nobody), and an image problem holds back all of it. A suspect with no adapter (no run_completeness
  row) is listed apart, unattributed: never re-fetched or held back.

It never builds a release, records a build or serves. Its one write is its job_manifests rows (job `fetch-health`),
whose detail holds the verdict and the list, for the owner to read; it publishes them to the dataset (Lake.publish,
as every job publishes its rows), so a later pass on another instance reads the same verdict. A failed publish is
logged and never changes the verdict or its exit code.

Re-fetching ships switched off: one setting, REFETCH_ENV (COFFEEDATA_DQ_REFETCH), unset by default, read by `crawl`
and `crawl-retry` (refetch_enabled). Off, no crawl pass reads the list, and the week's output is main's. Turning it
on changes which batch counts for the listed roasters, so it needs CLAUDE.md's evaluation first
(eval/run_quality/README.md). On, later crawl passes of the week (`crawl` for the resume and third slots,
`crawl-retry`) read the latest check that judged their connector and fetch its `empty` and `suspect` roasters again
(the whole catalog: no known count), unless fetched since that check or select_states would no longer crawl them
(opted out, no platform). A list that can't be read is skipped, never the pass. `failed` ones are listed for the
owner but need no hook: having no ok fetch, every crawl pass asks them again anyway, and crawl-retry deliberately
doesn't.

Triggered by its own Cloud Run job, polled by Cloud Scheduler every 20 minutes through Monday's crawl window: a poll
with no crawl attempt ended since the last check exits at once; otherwise it judges every connector whose pass has
ended (a `deadline` end row included), and leaves out one still running (the next poll after it ends judges it, and
the run as a whole). An attempt with only its running row is dead once a later attempt of the same job and connector
has started (never a manual `crawl --only` or an arm), or JOB_LIMIT after it started. A poll that finds another check
still running exits at once; a check with only its running row is dead CHECK_LIMIT after it started, or once a
later check has started.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import control, registry
from .lake import LAKE_TABLES, SCHEMA_FILES, Lake, retry_transient

log = logging.getLogger(__name__)

JOB = "fetch-health"
CRAWL_JOBS = ("crawl", "crawl-retry")  # the passes whose output it judges
# A crawl attempt with only its running row this long after it started has died: the crawl jobs' Cloud Run task
# timeout (ingestion.tf, 14400 s). Before that, it may still be running, and the check waits for it.
JOB_LIMIT = timedelta(minutes=240)
# A check's own running row this long after it started belongs to a check that died (killed, out of memory): a check
# takes minutes (its dbt step about 20 s on the replay copy and about 70 s on the whole lake, plus the overlay copy
# and the verdict), so 30 minutes is over 20 times that. It is also longer than the 20-minute poll interval, so a slow
# but live check is never overlapped by the next poll, while a killed one holds back at most the next poll, not the
# next 4 hours. A later check that has started also shows an earlier one with only its running row has died.
CHECK_LIMIT = timedelta(minutes=30)
GATE_TESTS = ("run_ok_rate", "adapter_ok_rate", "suspect_runs_need_review")
SELECT = ["+run_completeness", "+stg_run_quality", *GATE_TESTS]
FLAGS = (
    "--store-failures",  # each test's failing rows, which the verdict reads
    "--indirect-selection",
    "empty",  # only the tests named in SELECT, not every test on the models it builds
    "--warn-error-options",
    '{"error": ["NoNodesForSelectionCriteria"]}',  # a renamed model or test fails loudly instead of selecting nothing
)
# Why a roaster is on the list: only a named rule of the gate (one of GATE_TESTS), never an ad hoc check. Each reason
# is the rule whose failing (or not-ok) rows it is, and the entry records that rule by name. All three judge the
# fetch (did it work, did it bring back about as many products as usual), not the data in it.
RULES = {
    "failed": "run_ok_rate",  # a not-ok row of run_ok_rate's population: fetch failed (adapter_ok_rate's too)
    "empty": "run_ok_rate",  # a not-ok row of run_ok_rate's population: ok, but no products for a known catalog
    "suspect": "suspect_runs_need_review",  # a failing row of the test (stg_run_quality's suspect rule)
}
assert set(RULES.values()) <= set(GATE_TESTS)
# The one setting for re-fetching: crawl passes re-fetch the list's empty and suspect roasters only when it is on. Off by
# default (unset): the list is written for the owner, and no pass reads it. Turning it on needs CLAUDE.md's
# evaluation (eval/run_quality/README.md).
REFETCH_ENV = "COFFEEDATA_DQ_REFETCH"
OVERLAY_TABLES = ("fetches", "run_manifests")
DIR = Path("build/fetch-health")  # scratch: the copy of the crawl output, the database, dbt's artifacts


class CheckError(RuntimeError):
    """dbt couldn't build or run what the check needs: no verdict, which is not the same as a failed fetch."""


def refetch_enabled() -> bool:
    """Whether crawl and crawl-retry re-fetch the fetch-health check's list (REFETCH_ENV). Off unless set to
    1, true, yes or on."""
    return os.environ.get(REFETCH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def overlay(lake: Lake, week: str, dest: Path) -> dict[str, int]:
    """Copy the week's connector partitions (fetches and run_manifests) into `dest` as a lake root dbt can read,
    with every table's schema file so each of dbt's globs resolves. Files per connector. Copies, not links: a crawl
    that starts meanwhile may compact or remove its partition. A slice sub-partition (a confirmation arm) is never
    copied, as the merge never reads one."""
    from .connectors import _partition_files

    shutil.rmtree(dest, ignore_errors=True)
    for table in LAKE_TABLES:
        src = lake.root / SCHEMA_FILES[table]
        if src.exists():
            (dest / SCHEMA_FILES[table]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / SCHEMA_FILES[table])
    copied: dict[str, int] = {}
    for part in sorted((lake.root / "raw").glob(f"connector=*/run_date={week}")):
        connector = part.parent.name.removeprefix("connector=")
        for table in OVERLAY_TABLES:
            out = dest / table / f"run_date={week}"
            out.mkdir(parents=True, exist_ok=True)
            files = _partition_files(part, table)
            # Checkpoints first, then the compacted file: a compaction in between leaves rows twice (harmless,
            # every judged column is an aggregate or the latest batch), never missing.
            for i, f in enumerate([*files[1:], *files[:1]] if files and files[0].parent == part else files):
                try:
                    shutil.copyfile(f, out / f"{connector}-{i:05d}.parquet")
                except FileNotFoundError:  # compacted away meanwhile: the compacted file has its rows
                    continue
                copied[connector] = copied.get(connector, 0) + 1
    return copied


def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def judge(db: Path, results: list[dict], running: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The verdict from the scratch database `db` and dbt's run_results: what's systemic, and the repull list.
    running: connectors with a crawl attempt still running, left out (judged once their pass ends), and while any
    runs, the run as a whole isn't judged."""
    import duckdb

    from .quality import failures_table

    statuses = {r.get("unique_id"): r.get("status") for step in results for r in step.get("results") or []}
    broken = sorted(uid for uid, st in statuses.items() if st in ("error", "skipped"))
    tests = {uid.split(".")[-1]: st for uid, st in statuses.items() if uid and uid.startswith("test.")}
    if broken or not results or any(not r for r in results) or not set(GATE_TESTS) <= tests.keys():
        # A metric error is not a data failure (GX-3): no verdict, and the job fails so someone looks.
        raise CheckError(f"dbt didn't run the check: {', '.join(broken) or 'no results for every step'}")
    con = duckdb.connect(str(db), read_only=True)
    try:
        failing: dict[str, list[dict]] = {}
        for test in GATE_TESTS:
            table = failures_table(con, test)
            failing[test] = _rows(con, f"select * from {table}") if table else []
            # A test reported failing with no stored rows was never evaluated (GX: an empty result is a metric
            # error, not a finding); one reported passing with stored rows is just as unreadable. No verdict.
            if (tests[test] in ("fail", "warn")) != bool(failing[test]):
                raise CheckError(f"{test} says {tests[test]} but stored {len(failing[test])} failing rows")
        suspects = []
        if table := failures_table(con, "suspect_runs_need_review"):
            suspects = _rows(
                con,
                f"""select f.roaster_id, rc.adapter, f.products, f.baseline_products
                    from {table} f left join run_completeness rc using (roaster_id)""",
            )
        listed = _rows(
            con,
            # A partial fetch is never a reason, even one that read 0 products (a page block before the first
            # page): it is listed apart, under `partial`, with its reason.
            """select roaster_id, adapter, case when ok then 'empty' else 'failed' end as reason, challenge
               from run_completeness where has_catalog and attempted and not catalog_ok and not partial""",
        )
        # Main's other fetch states, listed apart: never a reason, never re-fetched by this check (module doc).
        partial = _rows(
            con,
            """select roaster_id, adapter, partial_reason as reason from run_completeness where partial
               order by adapter, roaster_id""",
        )
        quarantined = _rows(
            con,
            "select roaster_id, adapter from run_completeness where quarantined order by adapter, roaster_id",
        )
        reached = _rows(
            con,
            """select adapter, count(*) as expected, count(*) filter (where attempted) as attempted,
                      count(*) filter (where has_catalog and attempted) as judged,
                      count(*) filter (where has_catalog and attempted and catalog_ok) as ok,
                      count(*) filter (where partial) as partial,
                      count(*) filter (where quarantined) as quarantined,
                      count(*) filter (where challenge is not null) as challenged
               from run_completeness group by adapter order by adapter""",
        )
    finally:
        con.close()
    platforms = sorted((r for r in failing["adapter_ok_rate"] if r["adapter"] not in running),
                       key=lambda r: r["adapter"])  # fmt: skip
    down = [r["adapter"] for r in platforms]
    systemic = [
        {"scope": "connector", "connector": r["adapter"], "rule": "adapter_ok_rate", "roasters": r["roasters"],
         "ok_share": round(r["ok_share"], 3)}
        for r in platforms
    ]  # fmt: skip
    if len(down) >= 2:
        systemic.append({"scope": "image", "connectors": down, "rule": "adapter_ok_rate"})
    if failing["run_ok_rate"] and not down and not running:
        r = failing["run_ok_rate"][0]
        systemic.append({"scope": "run", "rule": "run_ok_rate", "roasters": r["roasters"],
                         "ok_share": round(r["ok_share"], 3)})  # fmt: skip
    image = len(down) >= 2
    reasons: dict[str, dict] = {}
    for r in listed + [s | {"reason": "suspect"} for s in suspects]:
        if r["adapter"] in running:
            continue
        e = reasons.setdefault(r["roaster_id"], {"roaster_id": r["roaster_id"], "adapter": r["adapter"],
                                                "reasons": []})  # fmt: skip
        e["reasons"].append(r["reason"])
        e["rules"] = sorted({*e.get("rules", []), RULES[r["reason"]]})
        if r.get("challenge"):
            e["challenge"] = r["challenge"]
        if r["reason"] == "suspect":
            e["products"], e["baseline_products"] = r["products"], r["baseline_products"]
    repull, held, unattributed = [], 0, []
    for e in sorted(reasons.values(), key=lambda e: (e["adapter"] or "", e["roaster_id"])):
        if e["adapter"] is None:
            # A suspect with no run_completeness row has no connector to ask it again: listed for the owner on its
            # own, whatever is systemic, and neither re-fetched nor counted as held back.
            unattributed.append(e)
        elif image or e["adapter"] in down:
            held += 1
        else:
            repull.append(e)
    return {
        "systemic": systemic,
        "repull": repull,
        # listed roasters kept off the list because their platform or the image is systemic (a challenged roaster is
        # never held back: it stays on the list, with its edge named)
        "held_back": held,
        "unattributed": unattributed,  # listed roasters with no adapter: for the owner, never re-fetched or held back
        # main's other fetch states, for the owner: never a reason, never re-fetched by this check, never held back
        "partial": [r for r in partial if r["adapter"] not in running],
        "quarantined": [r for r in quarantined if r["adapter"] not in running],
        "by_connector": reached,
        "running": sorted(running),
        "tests": {t: tests[t] for t in GATE_TESTS},  # pass, fail (an error-severity test) or warn
    }


def finding_key(f: dict) -> tuple[str, tuple[str, ...]]:
    """What makes a systemic finding the same one again: its scope and the connectors it names (none for `run`)."""
    names = f.get("connectors") or ([f["connector"]] if f.get("connector") else [])
    return f.get("scope", ""), tuple(sorted(names))


def run(lake: Lake, day: str, force: bool = False) -> dict[str, Any]:
    """One poll: judge the week's crawl output if a crawl pass has ended since the last check (or `force`)."""
    from .cli import dbt_build
    from .connectors import week_of

    week = week_of(day)
    # The poll's first read of the lake, retried on a transient error like every first read of a crawl: one dropped
    # connection must not fail the execution (and email) when there may be nothing to judge.
    rows = retry_transient(control.rows, lake, week)
    crawls = [r for r in rows if r["job"] in CRAWL_JOBS and not control.is_arm(r)]
    now = _utc(control.now())
    # An attempt with only its running row is live until JOB_LIMIT, unless a later attempt of the same job and
    # connector has started: that one is the retry (Cloud Run's, or the next slot), so the first has died, and its
    # connector is judged now rather than up to 4 hours later. A manual `crawl --only` (its detail says `only`) runs
    # beside a pass and is never its retry, nor is an arm (already left out of `crawls`).
    passes = [o for o in crawls if not control.detail_of(o).get("only")]
    later = {
        r["attempt_id"]
        for r in crawls
        if any(
            o["job"] == r["job"]
            and o["connector"] == r["connector"]
            and _utc(o["started_at"]) > _utc(r["started_at"])
            for o in passes
        )
    }
    live = [
        r
        for r in crawls
        if r["status"] == "running"
        and _utc(r["started_at"]) + JOB_LIMIT > now
        and r["attempt_id"] not in later
    ]
    died = [r for r in crawls if r["status"] == "running" and r not in live]
    # The attempts that have ended: an end row, or only a running row older than the time limit (it died).
    ended = sorted(r["attempt_id"] for r in crawls if r not in live)
    running = frozenset(r["connector"] or "" for r in live)
    # A failed check counts as having seen its attempts too: a dbt problem alerts once, not on every poll after
    # (`fetch-health --force` checks again once it's fixed).
    checks = [r for r in rows if r["job"] == JOB and r["status"] in ("done", "failed")]
    # Another check still running: it judges what has ended, and a second one meanwhile could alert twice for the same
    # finding. A check's running row counts as dead past CHECK_LIMIT, or once a later check has started.
    starts = [_utc(r["started_at"]) for r in rows if r["job"] == JOB]
    busy = [
        r
        for r in rows
        if r["job"] == JOB
        and r["status"] == "running"
        and _utc(r["started_at"]) + CHECK_LIMIT > now
        and not any(t > _utc(r["started_at"]) for t in starts)
    ]
    if not force:
        if busy:
            return {"run_date": week, "status": "up to date: another check is still running",
                    "running": sorted(running)}  # fmt: skip
        if not ended:
            return {"run_date": week, "status": "waiting: no crawl attempt has ended this week",
                    "running": sorted(running)}  # fmt: skip
        seen = {a for r in checks for a in control.detail_of(r).get("attempts", [])}
        if set(ended) <= seen:
            return {"run_date": week, "status": "up to date: no crawl attempt has ended since the last check",
                    "running": sorted(running)}  # fmt: skip
    job = control.start(lake, JOB, week)
    try:
        copied = overlay(lake, week, DIR / "overlay")
        db = DIR / "dq.duckdb"
        db.unlink(missing_ok=True)
        _, results = dbt_build(
            lake, db, {"as_of_run": week, "attempted_only": True}, select=SELECT, target=DIR / "target",
            extra_roots=(str(DIR / "overlay"),), flags=FLAGS,
        )  # fmt: skip
        out = judge(db, results, running)
    except Exception as e:
        control.finish(
            lake, job, "failed", detail={"error": f"{type(e).__name__}: {e}"[:500], "attempts": ended}
        )
        raise
    # One systemic finding alerts once a week: a later pass's check that finds it again (the same scope on the same
    # connectors) records it without alerting again. A new or different finding alerts.
    before = {
        finding_key(f)
        for r in checks
        if r["status"] == "done"
        for f in control.detail_of(r).get("systemic", [])
    }
    out = {
        "partitions": copied,
        "died": sorted({r["connector"] or "" for r in died}),
        "attempts": ended,
    } | out
    out["alert"] = [f for f in out["systemic"] if finding_key(f) not in before]
    control.finish(lake, job, "done", detail=out)
    # The verdict goes to the dataset, so the owner, and a later pass on another instance with its own staging folder,
    # can read it. A failed publish never changes the verdict or its exit code: the rows stay staged for the next
    # publish, and the execution's log has the verdict either way.
    try:
        lake.publish(f"fetch-health {week}")
    except Exception as e:  # noqa: BLE001 - the verdict stands
        log.warning("fetch-health %s: the verdict wasn't published: %s: %s", week, type(e).__name__, e)
    return {"run_date": week, "status": "checked", **out}


def repull_states(lake: Lake, connector: str, week: str) -> list[registry.RoasterState]:
    """The repull list of the latest check that judged `connector` this week (one that ran while its crawl did left
    it out), less any roaster fetched since that check (a resume slot already asked it again) and the `failed` ones:
    what a later crawl pass of the week (crawl, crawl-retry) fetches again although it has an ok fetch, when
    refetch_enabled (the passes check it before calling this). Only roasters
    select_states would still crawl (ingest.crawlable: a platform, not opted out, ...) whose adapter is still
    `connector`. Its lake reads are retried on a transient error (retry_transient), as every first read of a crawl."""
    from .ingest import crawlable

    rows = retry_transient(control.rows, lake, week)
    checks = [r for r in rows if r["job"] == JOB and r["status"] == "done"]
    detail: dict[str, Any] = {}
    since = None
    for r in sorted(checks, key=lambda r: (r["started_at"], r["ended_at"]), reverse=True):
        d = control.detail_of(r)
        if connector not in d.get("running", []):
            detail, since = d, _utc(r["started_at"])
            break
    # A `failed` roaster has no ok fetch, so every crawl pass asks it again anyway, and crawl-retry deliberately
    # doesn't (a store that refused all day isn't re-asked in the evening): only a fetch that landed but came back
    # empty or short. Only reasons a named gate rule gives (RULES): an entry with anything else is never fetched.
    listed = {
        e["roaster_id"]
        for e in detail.get("repull", [])
        if e.get("adapter") == connector and (set(e.get("reasons", [])) & RULES.keys()) - {"failed"}
    }
    if not listed:
        return []
    q = f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ? and fetched_at > ?"
    fetched = retry_transient(lambda: lake.connect().execute(q, [date.fromisoformat(week), since]).fetchall())
    todo = listed - {r[0] for r in fetched}
    # Every listed roaster was fetched this run (that's how the check saw it): `tried`, as select_states has it.
    states = [
        s
        for s in registry.current(lake)
        if s.roaster.roaster_id in todo and s.roaster.adapter == connector and crawlable(s, todo)
    ]
    if states:
        log.info(
            "fetch-health re-fetch: %d %s roaster(s) from the check at %s", len(states), connector, since
        )
    return states
