"""Design B: one weekly job per connector, then a weekly merge into the raw-run shape dbt reads.

Services, one job each, timed by Cloud Scheduler and coordinated only through the control table (job_manifests):
- connector ingest (`coffeedata crawl --connector <name>`, and `crawl-retry --connector` for parked stores):
  extract and load only (ELT), its adapter's roasters, under its own deadline, into one partition of the raw zone,
  which dbt never reads:

    raw/connector=<name>/run_date=<week>/_parts/<table>/batch=<id>.parquet   checkpoints while the job runs
    raw/connector=<name>/run_date=<week>/<table>.parquet                     one file per table, after compaction

  Running the same command again resumes: it skips roasters with an ok fetch this week and compacts again.
- weekly merge (`coffeedata merge`): the connectors the control table says landed, unioned into one file per table
  under the week's run_date (fetches/run_date=<week>/batch=<id>.parquet, and so on): the raw-run shape dbt reads.
  Rows keep their own batch_id and fetched_at; the merged partitions' files move, unchanged, to
  archive/raw/connector=<name>/run_date=<week>/merge=<batch>/ (a bucket lifecycle rule deletes them after 30 days,
  and nothing else reads archive/); the week's control rows are rolled into one file.
- post-load enrichment (`coffeedata enrich-week`): after the merge, Claude's candidates from the raw rows.
- weekly build + gate, then publish: the existing `build` and `publish` steps.

The Tuesday jobs (merge, enrich-week, build) start at fixed times, and each of the last two first checks in the
control table that the one before it is done for the week, waiting until a deadline and failing if it isn't
(chain.py).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import random
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from . import control, crashguard, ingest
from .lake import SCHEMAS, Lake, new_batch_id, retry_transient
from .ratelimit import SHOPIFY_RPS, SQUARESPACE_RPS

log = logging.getLogger(__name__)

RAW_TABLES = ("run_manifests", "fetches", "snapshots", "bodies")  # what a connector job loads (ELT)


@dataclass(frozen=True)
class Connector:
    name: str  # the adapter it crawls (models.Roaster.adapter)
    weekday: int  # 0 = Monday: the day its job runs
    hour: int  # UTC hour of its first slot; a resume slot follows `retry_after_h` later the same day
    rps: float | None = (
        None  # a platform-wide request budget (ratelimit.PlatformLimiter), if the platform has one
    )
    workers: int = 32
    deadline_min: float = 225.0  # stop starting roasters here, leaving time to compact under a 240-min limit
    retry_after_h: int = 6
    # Where its Cloud Run job's requests leave from: "default" (Cloud Run's shared Google egress) or "dedicated"
    # (Direct VPC egress through Cloud NAT with a reserved IP: Shopify's own, or the one WooCommerce and generic
    # share; src/terraform/ingestion.tf, local.ingest_jobs). The job records it, with the IPs it saw, in
    # job_manifests. A per-connector setting, not a different design.
    egress: str = "default"
    # Pacing, with rps above and deadline_min (the crawl window: how long one job may run, up to its time limit):
    # stores in a shuffled order, seeded by the week, so the same stores aren't always the last ones asked.
    shuffle: bool = True


EGRESS_MODES = ("default", "dedicated")
CONNECTORS: dict[str, Connector] = {
    c.name: c
    for c in (
        Connector("shopify", 0, 9, rps=SHOPIFY_RPS, egress="dedicated"),  # the confirmation run compares
        Connector("squarespace", 0, 9, rps=SQUARESPACE_RPS),  # one shared budget, as Shopify's
        Connector("woocommerce", 0, 9, egress="dedicated"),  # SiteGround challenges default egress
        Connector("square_online", 0, 9),
        Connector("generic", 0, 9, egress="dedicated"),
    )
}
# The slots Cloud Scheduler runs (src/terraform/ingestion.tf, at minute :17; the weekly baseline is Monday 09:17 UTC).
MERGE_WEEKDAY, MERGE_HOUR = 1, 3  # Tuesday 03:17 UTC, after every connector's resume slot and crawl-retry
# The rest of the week, one job each, timed by Cloud Scheduler and gated by the control table:
ENRICH_HOUR = 4  # Tuesday 04:17: post-load enrichment (Claude candidates from raw), after the merge
BUILD_HOUR = 6  # Tuesday 06:17: classify verdicts, dbt build + gate, then publish (cli weekly's last steps)


# A test crawl's id (the admin's Jobs page, `crawl --test`): its sub-partition's name, test=<id>.
TEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def week_of(day: str) -> str:
    """The week's run_date: the Monday of `day`'s ISO week. Every connector's job that week writes under it."""
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def partition(lake: Lake, connector: str, week: str) -> Path:
    return lake.root / f"raw/connector={connector}/run_date={week}"


class PartitionLake(Lake):
    """The lake, as one connector job sees it: reads the lake plus its own partition, writes only the partition.

    slice ("k/n", the confirmation run's arms): the arm gets a sub-partition of its own, slice=k-of-n, so arms that
    run at the same time never compact each other's checkpoints. test (an id, the admin's test crawls): the same, in
    test=<id>. The weekly merge reads only the partition's own files, so an arm's or a test's rows are never merged."""

    def __init__(
        self, base: Lake, connector: str, week: str, slice: str | None = None, test: str | None = None
    ):
        super().__init__(base.url, staging=base.root if base.remote else None, tables=base.tables)
        self.part = partition(self, connector, week_of(week))
        if slice and test:
            raise ValueError("a crawl is a slice or a test, not both")
        if slice:
            self.part = self.part / f"slice={slice.replace('/', '-of-')}"
        if test:
            if not TEST_ID.fullmatch(test):
                raise ValueError(f"bad test id: {test!r}")
            self.part = self.part / f"test={test}"

    def write_partition(self, table: str, run_date: str, batch_id: str, rows: list[dict]) -> Path | None:
        rel = self.part.relative_to(self.root) / "_parts" / table / f"batch={batch_id}.parquet"
        return self.write(table, str(rel), rows)

    def sources(self, table: str) -> list[str]:
        out = super().sources(table)
        if table in RAW_TABLES:
            out += [str(p) for p in _partition_files(self.part, table)]
        return out


def _partition_files(part: Path, table: str) -> list[Path]:
    one = part / f"{table}.parquet"
    return ([one] if one.exists() else []) + sorted((part / "_parts" / table).glob("*.parquet"))


def archive(lake: Lake, part: Path, batch: str) -> Path:
    """Where the merge `batch` keeps a connector partition's merged files: archive/ + the partition's own path +
    merge=<batch>, in the same bucket. No reader globs archive/ (dbt, resume, the merge and the fetch-health check all
    read raw/), and the staging bucket's lifecycle rule deletes it 30 days after the merge (src/terraform)."""
    return lake.root / "archive" / part.relative_to(lake.root) / f"merge={batch}"


def _archive_files(part: Path, files: list[Path], dest: Path) -> None:
    """Move a partition's files to dest by rename, at the same relative paths, so their bytes are the ones the merge
    read. On GCSFuse a rename is a copy and a delete (no chmod or utime, which GCSFuse refuses): the archived object
    is new, so the lifecycle rule's age starts at the merge."""
    for f in files:
        to = dest / f.relative_to(part)
        to.parent.mkdir(parents=True, exist_ok=True)
        os.replace(f, to)


def _write_table(path: Path, tbl: pa.Table) -> None:
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, tmp, compression="zstd")
    tmp.replace(path)


def _read(files: list[Path], table: str) -> pa.Table:
    """The files' rows in the table's current schema. A file written before a column was added (an image from an
    older raw contract, e.g. without fetches.partial) reads that column as null."""
    schema = SCHEMAS[table]
    tbls = []
    for f in files:
        t = pq.read_table(f)
        for col in schema:
            if col.name not in t.column_names:
                t = t.append_column(col.name, pa.nulls(len(t), col.type))
        tbls.append(t.select(schema.names).cast(schema))
    return pa.concat_tables(tbls) if tbls else schema.empty_table()


# The column a checkpoint file's rows all carry its batch id in (write_partition's batch=<id>.parquet). bodies has
# none: its rows are keyed by body_hash.
BATCH_KEYS = {"run_manifests": "manifest_id", "fetches": "batch_id", "snapshots": "batch_id"}


def _not_yet_compacted(one: Path, parts: list[Path], table: str) -> pa.Table:
    """The rows of `parts` that the compacted file `one` doesn't already hold. A job killed after writing `one` but
    before removing its parts leaves both, and the next compact must not add those rows twice."""
    done = pq.read_table(one, columns=[BATCH_KEYS.get(table, "body_hash")]).column(0)
    seen = set(done.to_pylist())
    if table in BATCH_KEYS:
        return _read([f for f in parts if f.stem.removeprefix("batch=") not in seen], table)
    new = _read(parts, table)
    return new.take([i for i, h in enumerate(new.column("body_hash").to_pylist()) if h not in seen])


def compact(part: Path) -> dict[str, int]:
    """Fold the partition's checkpoint files into one file per table (rows kept as they are). Idempotent: if a run
    dies between writing the compacted file and removing the parts, the next one skips the parts already in it."""
    out = {}
    for table in RAW_TABLES:
        files = _partition_files(part, table)
        if not files:
            continue
        one = part / f"{table}.parquet"
        parts = [f for f in files if f.parent != part]
        if parts:
            tbl = _not_yet_compacted(one, parts, table) if one.exists() else _read(parts, table)
            _write_table(one, pa.concat_tables([_read([one], table), tbl]) if one.exists() else tbl)
            for f in parts:
                f.unlink()
        out[table] = pq.read_metadata(one).num_rows
    shutil.rmtree(part / "_parts", ignore_errors=True)
    return out


def in_slice(key: str, part: str, salt: str) -> bool:
    """Whether a key falls in slice "k/n" of a salted-hash split (the confirmation run's arms). crawl passes the
    roaster's host, so roasters that share a host (one store under several domains) are always in the same arm."""
    k, n = (int(x) for x in part.split("/"))
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest(), 16) % n == k


def _finished(lake: Lake, week: str) -> set[str]:
    """The week's attempts with a done or deadline end row: everything else under _inflight died."""
    rows = retry_transient(control.rows, lake, week)
    return {r["attempt_id"] for r in rows if r["status"] in ("done", "deadline")}


def _window_left(lake: Lake, job: str, connector: str | None, week: str, deadline_min: float) -> float:
    """Minutes left of the window of this Cloud Run execution: its first task attempt's start + deadline_min. A
    retried attempt (max_retries 1) resumes inside the window the first one had, so it ends before the next slot
    (the 15:17 resume, or the merge) instead of overlapping it. The full deadline for a first attempt, or outside
    Cloud Run."""
    starts = [r["started_at"] for r in control.this_execution(lake, week, job, connector)]
    if not starts:
        return deadline_min
    first = min(starts)
    first = first if first.tzinfo else first.replace(tzinfo=UTC)
    return max(0.0, deadline_min - (control.now() - first).total_seconds() / 60)


def _ok_errors(out: dict) -> tuple[int, int]:
    """job_manifests' ok and errors for an ingest.run: a quarantined roaster is neither (never fetched; its own
    count in by_adapter, as the dbt ok rates leave its `crashed` row out)."""
    by = (out.get("by_adapter") or {}).values()
    errors = sum(a.get("errors", 0) for a in by)
    return sum(a.get("roasters", 0) - a.get("quarantined", 0) for a in by) - errors, errors


def crawl(
    lake: Lake,
    connector: str,
    day: str,
    deadline_min: float | None = None,
    rps: float | None = None,
    part: str | None = None,
    salt: str = "arm",
    only: str | None = None,
    test: str | None = None,
) -> dict:
    """One connector's weekly job, or its resume: fetch its roasters without an ok fetch this week (parked stores
    continue from their blocked page), then compact. Extract and load only. A job_manifests row marks its start
    and its end: an attempt with only the first one died (killed or timed out), and one that raised ends `failed`.
    rps overrides the connector's budget, and part ("k/n", with salt) crawls one slice of its roasters: both for the
    confirmation run's arms. only: fetch that one roaster, even if done or quarantined this week (RUNBOOK §7).
    test (an id): a test crawl (the admin's Jobs page): it writes to its own test=<id> sub-partition, which the
    merge never reads, and its rows say arm "test", so it never counts as the week's crawl (control.is_arm).

    The poison-pill guard (crashguard): the roasters in flight when an earlier attempt of this week died are fetched
    one at a time; one in flight in two dead attempts is quarantined for the week (a `crashed` fetches row)."""
    c = CONNECTORS[connector]
    if rps is not None:
        c = dataclasses.replace(c, rps=rps)
    week = week_of(day)
    plake = PartitionLake(lake, connector, week, slice=part, test=test)
    compact(plake.part)  # a killed attempt's checkpoints, before deciding what is left
    if test and not only:
        states, mode = _test_states(plake, connector), "full"
    else:
        states, mode = ingest.select_states(plake, week, only=only)
    states = [s for s in states if s.roaster.adapter == connector]
    repulls: list = []
    if not (
        part or only or test
    ):  # the fetch-health list, with re-fetch on (never for an arm, a test or --only)
        repulls = _repulls(plake, connector, week, states)
        states += repulls
    if part:
        states = [s for s in states if in_slice(s.roaster.host.lower() or s.roaster.roaster_id, part, salt)]
    if c.shuffle:
        random.Random(f"{connector}:{week}").shuffle(states)
    todo_ids = {s.roaster.roaster_id for s in states}
    health = crashguard.assess(crashguard.root(plake.part), _finished(lake, week))
    quarantine = {} if only else health.quarantine(todo_ids)
    window = _window_left(lake, "crawl", connector, week, deadline_min or c.deadline_min)
    # the quarantine this attempt applies: none for --only, so its start row doesn't list the roaster it fetches
    started = control.execution() | health.summary(todo_ids) | {"quarantined": sorted(quarantine)}
    # A manual one-roaster run (RUNBOOK §7) says so: never a retry of the pass running beside it (fetchhealth.run).
    if only:
        started["only"] = only
    if test:  # is_arm keeps it out of `landed`, the egress flags and fetch-health from the start row on
        started |= {"test": test, "arm": "test"}
    job = control.start(lake, "crawl", week, connector, len(states), detail=started)
    guard = crashguard.Guard(crashguard.root(plake.part), job["attempt_id"])
    if health.dead:
        log.warning("crawl %s: %d earlier attempt(s) this week died; suspects %s, quarantined %s", connector,
                    len(health.dead), sorted(health.suspects), sorted(quarantine))  # fmt: skip
    try:
        out: dict[str, Any] = (
            ingest.run(
                plake,
                states,
                week,
                workers=c.workers,
                mode="only" if only else "full",
                shopify_rps=c.rps or SHOPIFY_RPS,
                platform_rps={connector: c.rps} if c.rps else None,  # its own shared budget, from config
                deadline_s=60 * window,
                resume={k: v for k, v in ingest.load_parked(plake, week).items() if k in todo_ids},
                guard=guard,
                suspects=set(health.suspects) - set(quarantine),
                quarantine=quarantine,
                refetch={s.roaster.roaster_id for s in repulls},
            )
            if states
            else {"roasters": 0, "by_adapter": {}, "skipped_deadline": 0, "deferred": {}}
        )
        rows = compact(plake.part)
    except Exception as e:
        # The end row says why; the in-flight markers stay, so the next attempt sees what was running.
        control.finish(lake, job, "failed", detail=started | {"error": f"{type(e).__name__}: {e}"[:500]})
        raise
    ok, errors = _ok_errors(out)
    detail = {"rows": rows, "by_adapter": out.get("by_adapter"), "deferred": out.get("deferred"),
              "partial": out.get("partial", 0),
              # stores partial in each of their last runs (ingest.PARTIAL_WEEKS): RUNBOOK §5.3
              "partial_every_week": out.get("partial_every_week") or [],
              # the job's own limiter (requests, 429s, pauses), as shopify_limiter, squarespace_limiter...
              **({f"{connector}_limiter": (out.get("limiters") or {}).get(connector)} if c.rps else {}),
              # the network the job actually ran on (set by the deploy), else the connector's config
              "egress": os.environ.get("COFFEEDATA_EGRESS_MODE") or c.egress,
              # the confirmation run's arm (COFFEEDATA_ARM), budget and slice, if any
              "rps": c.rps, "slice": part, "salt": salt if part else None,
              "arm": "test" if test else os.environ.get("COFFEEDATA_ARM"), "only": only, "test": test,
              # the Cloud Run execution and task attempt, and the poison-pill guard's view at the start
              **started, "window_min": round(window, 1)}  # fmt: skip
    end = control.finish(
        # deadline: roasters left unstarted, or catalogs it stopped mid-way (partial): the resume slot reads them
        lake, job, "deadline" if out.get("skipped_deadline") or out.get("partial") else "done",
        ok=ok, errors=errors, parked=(out.get("deferred") or {}).get("page_parked", 0),
        skipped=out.get("skipped_deadline", 0), requests=out.get("requests"), http_429=out.get("http_429"),
        http_430=out.get("http_430"), first_contact=out.get("first_contact"),
        detail=detail,
    )  # fmt: skip
    guard.close()  # after the end row: a kill in between leaves a folder assess() removes
    lake.publish(f"crawl {connector} {week}")
    return {"connector": connector, "run_date": week, "todo": len(states), **detail,
            "status": end["status"], "skipped_deadline": out.get("skipped_deadline", 0)}  # fmt: skip


def _test_states(plake: PartitionLake, connector: str) -> list:
    """A test crawl's roasters: every crawlable roaster of the connector, whatever the week's own crawl (merged or
    not) already fetched, less the ones this test's own partition fetched whole (a resume of the same test). A test
    crawl re-checks the stores; it never reads as "nothing left to do" because the week is done."""
    from . import registry

    done: set[str] = set()
    files = _partition_files(plake.part, "fetches")
    if files:
        tbl = _read(files, "fetches").select(["roaster_id", "status", "partial"]).to_pylist()
        done = {r["roaster_id"] for r in tbl if r["status"] in ("unavailable", "crashed") or
                (r["status"] == "ok" and not r["partial"])}  # fmt: skip
    return [s for s in registry.current(plake) if s.roaster.adapter == connector
            and ingest.crawlable(s, frozenset()) and s.roaster.roaster_id not in done]  # fmt: skip


def _repulls(plake: Lake, connector: str, week: str, states: list) -> list:
    """The roasters the fetch-health check (fetchhealth) listed to fetch again that `states` doesn't already have,
    only when re-fetch is on (fetchhealth.refetch_enabled, off by default): off, the list isn't even read, and the
    pass is main's.
    A repull is an extra: if it can't be read (the lake still failing after retry_transient's retries, an
    unreadable check), it is logged and skipped, and the crawl pass goes on with its own work."""
    from . import fetchhealth

    if not fetchhealth.refetch_enabled():
        return []
    have = {s.roaster.roaster_id for s in states}
    try:
        listed = fetchhealth.repull_states(plake, connector, week)
    except Exception as e:  # noqa: BLE001 - never the pass's failure
        log.warning("crawl %s: the fetch-health check's list skipped: %s: %s", connector, type(e).__name__, e)
        return []
    return [s for s in listed if s.roaster.roaster_id not in have]


def crawl_retry(
    lake: Lake,
    connector: str,
    day: str,
    workers: int = 8,
    deadline_min: float | None = None,
    test: str | None = None,
) -> dict:
    """crawl-retry for one connector: its parked stores (blocked mid-catalog, Shopify's page blocks), resumed from
    their blocked page, and, only with re-fetch on (fetchhealth.refetch_enabled), the fetch-health check's list (a
    fetch that landed but came back empty or short: empty or suspect).
    Only those: a store that failed outright is not asked again here, so a Shopify store that refused all day isn't
    re-asked in the evening (the connectors that park nothing get their third pass from
    another `crawl`, which resumes and retries everything without an ok fetch). With job_manifests rows (job
    crawl-retry, which never counts as a landed crawl) and the poison-pill guard, as crawl. Like crawl, it stops
    starting stores at deadline_min (default the connector's, under the job's 240-minute limit). test (an id): its
    test crawl's own test=<id> sub-partition, as crawl's."""
    week = week_of(day)
    plake = PartitionLake(lake, connector, week, test=test)
    compact(plake.part)
    states, resume = ingest.select_parked(plake, week, adapter=connector)
    repulls = [] if test else _repulls(plake, connector, week, states)  # fetched whole: no parked progress
    states += repulls
    ids = {s.roaster.roaster_id for s in states}
    health = crashguard.assess(crashguard.root(plake.part), _finished(lake, week))
    quarantine = health.quarantine(ids)
    started = control.execution() | health.summary(ids) | ({"test": test, "arm": "test"} if test else {})
    window = _window_left(
        lake, "crawl-retry", connector, week, deadline_min or CONNECTORS[connector].deadline_min
    )
    job = control.start(lake, "crawl-retry", week, connector, len(states), detail=started)
    guard = crashguard.Guard(crashguard.root(plake.part), job["attempt_id"])
    try:
        out = ingest.run(plake, states, week, workers=workers, mode="retry_failed", resume=resume, guard=guard,
                         suspects=set(health.suspects) - set(quarantine), quarantine=quarantine,
                         refetch={s.roaster.roaster_id for s in repulls}, deadline_s=60 * window)  # fmt: skip
        compact(plake.part)
    except Exception as e:
        control.finish(lake, job, "failed", detail=started | {"error": f"{type(e).__name__}: {e}"[:500]})
        raise
    ok, errors = _ok_errors(out)
    control.finish(
        # deadline, as crawl's: roasters left unstarted, or catalogs it stopped mid-way again (partial)
        lake, job, "deadline" if out.get("skipped_deadline") or out.get("partial") else "done", ok=ok,
        errors=errors, parked=(out.get("deferred") or {}).get("page_parked", 0), requests=out.get("requests"),
        http_429=out.get("http_429"), http_430=out.get("http_430"), first_contact=out.get("first_contact"),
        skipped=out.get("skipped_deadline", 0),
        detail=started | {"by_adapter": out.get("by_adapter"), "deferred": out.get("deferred"),
                          "partial": out.get("partial", 0),
                          # stores partial in each of their last runs (ingest.PARTIAL_WEEKS): RUNBOOK §5.3
                          "partial_every_week": out.get("partial_every_week") or [],
                          "egress": os.environ.get("COFFEEDATA_EGRESS_MODE") or CONNECTORS[connector].egress},
    )  # fmt: skip
    guard.close()
    lake.publish(f"crawl-retry {connector} {week}")
    return out


def merge(lake: Lake, day: str) -> dict:
    """Union every connector's partition for the week into the lake's own tables, one file per table. Which
    connectors landed comes from the control table (job_manifests), not from the other jobs."""
    week = week_of(day)
    job = control.start(lake, "merge", week)
    try:
        return _merge_week(lake, week, job)
    except Exception as e:
        # An end row for a merge that raised, as the crawls write: the control table then tells "raised" (failed,
        # with the error) from "killed" (a start row alone). Either way the jobs after it don't run on this week.
        control.finish(lake, job, "failed", detail={"error": f"{type(e).__name__}: {e}"[:500]})
        raise


def _merge_week(lake: Lake, week: str, job: dict) -> dict:
    con = lake.connect()
    landed = control.landed(lake, week)
    present = {c: partition(lake, c, week) for c in sorted(landed) if partition(lake, c, week).exists()}
    merged_before: set[str] = set()  # by an earlier merge this week
    for r in control.rows(lake, week):
        if r["job"] == "merge" and r["status"] == "done":
            merged_before |= set(json.loads(r["detail"] or "{}").get("connectors", []))
    missing = sorted(set(CONNECTORS) - landed - merged_before)
    for p in present.values():
        compact(p)
    batch = new_batch_id()
    written = {}
    merged: dict[str, list[Path]] = {c: [] for c in present}  # the files read, archived as they are
    for table in RAW_TABLES:
        files = {c: _partition_files(p, table) for c, p in present.items()}
        for c, fs in files.items():
            merged[c] += fs
        tbl = _read([f for fs in files.values() for f in fs], table)
        if table == "run_manifests" and missing:
            # A connector whose job never landed still belongs to the week's expected set: its roasters show up in
            # run_completeness as not attempted, instead of the run looking complete without them.
            states, _ = ingest.select_states(lake, week)
            gone = [s for s in states if s.roaster.adapter in missing]
            # One id per week, not per merge: the check below then adds these rows once, however often the week
            # is merged (a per-merge id stored them again on every re-run).
            rows = ingest.manifest_rows(
                [s.roaster for s in gone], date.fromisoformat(week), f"missing-{week}",
                {s.roaster.roaster_id: s.status for s in gone}, "full", None,
            )  # fmt: skip
            tbl = pa.concat_tables([tbl, pa.Table.from_pylist(rows, schema=SCHEMAS[table])])
        # A re-run merge (a late connector) adds only what the lake doesn't have yet.
        con.register("new", tbl)
        key = {"run_manifests": "manifest_id", "fetches": "batch_id", "snapshots": "batch_id"}.get(table)
        if table == "run_manifests":
            # By manifest id, as before; the merge's own "not attempted" rows (one id per week, missing-<week>) by
            # id and roaster, so a re-merge still adds those of roasters registered after the first merge.
            q = f"""select * from new n where not exists (
                        select 1 from {lake.scan(table)} s where s.manifest_id = n.manifest_id
                        and (n.manifest_id not like 'missing-%' or s.roaster_id is not distinct from n.roaster_id))"""
        elif key:
            q = f"select * from new where {key} not in (select distinct {key} from {lake.scan(table)} where {key} is not null)"
        else:
            q = f"select * from new where body_hash not in (select body_hash from {lake.scan('bodies')})"
        new = con.sql(q).to_arrow_table().select(SCHEMAS[table].names).cast(SCHEMAS[table])
        con.unregister("new")
        if not new.num_rows:
            continue
        path = lake.root / f"{table}/run_date={week}/batch={batch}.parquet"
        if path.exists():
            raise FileExistsError(path)
        _write_table(path, new)
        written[table] = new.num_rows
    for c, p in present.items():  # the week's data now lives in the lake's tables; resumes read it from there
        _archive_files(p, merged[c], archive(lake, p, batch))  # kept 30 days, out of every reader's way
        shutil.rmtree(p)  # anything left was never merged (an arm's slice=*/)
        if not any(p.parent.iterdir()):
            p.parent.rmdir()
    control.finish(lake, job, "done", detail={"batch": batch, "written": written, "missing": missing,
                                             "connectors": sorted(present)})  # fmt: skip
    control.rollup(lake, week)  # the week's control rows, one file
    lake.publish(f"merge {week}")
    return {
        "run_date": week,
        "batch": batch,
        "written": written,
        "missing": missing,
        "connectors": sorted(present),
    }


def enrich_week(
    lake: Lake, day: str, limit: int = 2000, submit: bool = False, require_merge: bool = True
) -> dict:
    """The post-load enrichment job: after the week's merge (it reads that from the control table), Claude's
    candidates come from the raw rows (enrich.candidates, the build's own rules). It spends only when asked to
    (submit: `--submit` on the scheduled job) and it has credentials (enrich.collect + enrich.run); otherwise it
    only counts them. The CLI checks the merge first (chain.py: it waits, then fails loudly), so the `waiting`
    answer here is for callers of this function; require_merge=False is the owner's logged override."""
    from . import chain, enrich, extract

    week = week_of(day)
    # the chain's one definition of a done merge: its latest attempt this week, done and published
    if require_merge and chain.state(lake, week, "merge") != "done":
        return {"run_date": week, "status": "waiting: no merge this week yet"}
    job = control.start(lake, "enrich", week)
    out: dict[str, Any]
    if submit and extract.llm_enabled():
        out = {"collect": enrich.collect(lake, week), "enrich": enrich.run(lake, limit, week)}
    else:
        out = {
            "candidates": len(enrich.candidates(lake, limit)),
            "submitted": 0,
            "note": "no Anthropic credentials" if submit else "dry run: pass --submit to send",
        }
    control.finish(lake, job, "done", detail=out)
    lake.publish(f"enrich {week}")
    return {"run_date": week, **out}
