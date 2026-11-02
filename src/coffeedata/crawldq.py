"""Post-crawl data quality (`coffeedata crawl-dq`): the weekly gate's own rules, run on the week's crawl output
shortly after each crawl pass ends, before the merge, so a roaster whose data looks bad is fetched again the same
day and a systemic failure is known on Monday instead of at Tuesday's build.

What it runs is the gate, not a copy of it: dbt builds run_completeness and stg_run_quality (and what they read)
into a scratch database and runs three of the gate's singular tests with their failing rows stored:
- run_ok_rate and adapter_ok_rate, with var attempted_only (the roasters a crawl pass has reached: a roaster a pass
  left for its resume slot is not a failure yet), the same thresholds as the build;
- suspect_runs_need_review: stg_run_quality's suspect rule (fewer than half the median of the last 4 counted weeks).
dbt reads the lake plus a copy of the connector partitions (raw/connector=*/run_date=<week>/, which dbt's globs never
see) laid out as one more lake root. Only fetches and run_manifests are copied: every column judged here comes from
them (and earlier weeks); snapshots and bodies are what the build classifies.

What it decides, with no new threshold of its own:
- systemic: a platform below adapter_ok_rate's floor (scope connector); two or more platforms below it in the same
  pass, which points at what they share, the image, our code or its egress, rather than at one platform (scope
  image); the run below run_ok_rate's floor with no single platform to blame (scope run). The job exits non-zero,
  which emails the owner (alerts.tf, ingest_job_failed).
- repull: per roaster, the rows the gate counts as not ok, with the rule as the reason: `failed` (the fetch
  failed), `empty` (it succeeded with no products, for a roaster known to have a catalog), `suspect`. A platform
  judged systemic is held back from it (asking a platform that is refusing us again the same evening helps nobody),
  and an image problem holds back all of it. Anything the rules can't tell is left alone (CLAUDE.md: abstain).

It never builds a release, records a build, serves, or publishes. Its one write is its job_manifests rows (job
`dq`), whose detail holds the repull list. Later crawl passes of the week (`crawl` for the resume and third
slots, `crawl-retry`) read the latest check that judged their connector and fetch its `empty` and `suspect` roasters
again, unless fetched since that check. `failed` ones are listed for the owner but need no hook: having no ok fetch,
every crawl pass asks them again anyway, and crawl-retry deliberately doesn't.

Triggered by its own Cloud Run job, polled by Cloud Scheduler every 20 minutes through Monday's crawl window: a poll
with no crawl attempt ended since the last check exits at once; otherwise it judges every connector whose pass has
ended, and leaves out one still running (the next poll after it ends judges it, and the run as a whole).
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import control, registry
from .lake import LAKE_TABLES, SCHEMA_FILES, Lake

log = logging.getLogger(__name__)

JOB = "dq"
CRAWL_JOBS = ("crawl", "crawl-retry")  # the passes whose output it judges
# A crawl attempt with only its running row this long after it started has died: the crawl jobs' Cloud Run task
# timeout (ingestion.tf, 14400 s). Before that, it may still be running, and the check waits for it.
JOB_LIMIT = timedelta(minutes=240)
GATE_TESTS = ("run_ok_rate", "adapter_ok_rate", "suspect_runs_need_review")
SELECT = ["+run_completeness", "+stg_run_quality", *GATE_TESTS]
FLAGS = (
    "--store-failures",  # each test's failing rows, which the verdict reads
    "--indirect-selection",
    "empty",  # only the tests named in SELECT, not every test on the models it builds
    "--warn-error-options",
    '{"error": ["NoNodesForSelectionCriteria"]}',  # a renamed model or test fails loudly instead of selecting nothing
)
OVERLAY_TABLES = ("fetches", "run_manifests")
DIR = Path("build/crawl-dq")  # scratch: the copy of the crawl output, the database, dbt's artifacts


class CheckError(RuntimeError):
    """dbt couldn't build or run what the check needs: no verdict, which is not the same as bad data."""


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
        bad = _rows(
            con,
            """select roaster_id, adapter, case when ok then 'empty' else 'failed' end as reason
               from run_completeness where has_catalog and attempted and not catalog_ok""",
        )
        reached = _rows(
            con,
            """select adapter, count(*) as expected, count(*) filter (where attempted) as attempted,
                      count(*) filter (where has_catalog and attempted) as judged,
                      count(*) filter (where has_catalog and attempted and catalog_ok) as ok
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
        systemic.append(
            {"scope": "image", "connectors": down, "rule": "adapter_ok_rate on 2+ platforms at once"}
        )
    if failing["run_ok_rate"] and not down and not running:
        r = failing["run_ok_rate"][0]
        systemic.append({"scope": "run", "rule": "run_ok_rate", "roasters": r["roasters"],
                         "ok_share": round(r["ok_share"], 3)})  # fmt: skip
    image = len(down) >= 2
    reasons: dict[str, dict] = {}
    for r in bad + [s | {"reason": "suspect"} for s in suspects]:
        if r["adapter"] in running:
            continue
        e = reasons.setdefault(r["roaster_id"], {"roaster_id": r["roaster_id"], "adapter": r["adapter"],
                                                "reasons": []})  # fmt: skip
        e["reasons"].append(r["reason"])
        if r["reason"] == "suspect":
            e["products"], e["baseline_products"] = r["products"], r["baseline_products"]
    repull, held = [], 0
    for e in sorted(reasons.values(), key=lambda e: (e["adapter"] or "", e["roaster_id"])):
        if image or e["adapter"] in down:
            held += 1
        else:
            repull.append(e)
    return {
        "systemic": systemic,
        "repull": repull,
        "held_back": held,  # bad roasters not repulled because their platform (or the image) is systemic
        "by_connector": reached,
        "running": sorted(running),
        "tests": {t: tests[t] for t in GATE_TESTS},  # pass, fail (an error-severity test) or warn
    }


def run(lake: Lake, day: str, force: bool = False) -> dict[str, Any]:
    """One poll: judge the week's crawl output if a crawl pass has ended since the last check (or `force`)."""
    from .cli import dbt_build
    from .connectors import week_of

    week = week_of(day)
    rows = control.rows(lake, week)
    crawls = [r for r in rows if r["job"] in CRAWL_JOBS and not control.is_arm(r)]
    now = _utc(control.now())
    live = [r for r in crawls if r["status"] == "running" and _utc(r["started_at"]) + JOB_LIMIT > now]
    died = [r for r in crawls if r["status"] == "running" and r not in live]
    # The attempts that have ended: an end row, or only a running row older than the time limit (it died).
    ended = sorted(r["attempt_id"] for r in crawls if r not in live)
    running = frozenset(r["connector"] or "" for r in live)
    # A failed check counts as having seen its attempts too: a dbt problem alerts once, not on every poll after
    # (`crawl-dq --force` checks again once it's fixed).
    checks = [r for r in rows if r["job"] == JOB and r["status"] in ("done", "failed")]
    if not force:
        if not ended:
            return {"run_date": week, "status": "waiting: no crawl attempt has ended this week",
                    "running": sorted(running)}  # fmt: skip
        seen = {a for r in checks for a in json.loads(r["detail"] or "{}").get("attempts", [])}
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
    out = {
        "partitions": copied,
        "died": sorted({r["connector"] or "" for r in died}),
        "attempts": ended,
    } | out
    control.finish(lake, job, "done", detail=out)
    return {"run_date": week, "status": "checked", **out}


def repull_states(lake: Lake, connector: str, week: str) -> list[registry.RoasterState]:
    """The repull list of the latest check that judged `connector` this week (one that ran while its crawl did left
    it out), less any roaster fetched since that check (a resume slot already asked it again) and the `failed` ones:
    what a later crawl pass of the week (crawl, crawl-retry) fetches again although it has an ok fetch."""
    checks = [r for r in control.rows(lake, week) if r["job"] == JOB and r["status"] == "done"]
    detail, since = {}, None
    for r in sorted(checks, key=lambda r: (r["started_at"], r["ended_at"]), reverse=True):
        d = json.loads(r["detail"] or "{}")
        if connector not in d.get("running", []):
            detail, since = d, _utc(r["started_at"])
            break
    # A `failed` roaster has no ok fetch, so every crawl pass asks it again anyway, and crawl-retry deliberately
    # doesn't (a store that refused all day isn't re-asked in the evening): only data that landed but looks bad.
    listed = {
        e["roaster_id"]
        for e in detail.get("repull", [])
        if e.get("adapter") == connector and set(e.get("reasons", [])) - {"failed"}
    }
    if not listed:
        return []
    fetched = (
        lake.connect()
        .execute(
            f"select distinct roaster_id from {lake.scan('fetches')} where run_date = ? and fetched_at > ?",
            [date.fromisoformat(week), since],
        )
        .fetchall()
    )
    todo = listed - {r[0] for r in fetched}
    states = [
        s for s in registry.current(lake) if s.roaster.roaster_id in todo and s.roaster.adapter == connector
    ]
    if states:
        log.info("crawl-dq repull: %d %s roaster(s) from the check at %s", len(states), connector, since)
    return states
