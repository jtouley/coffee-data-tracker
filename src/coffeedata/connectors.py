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
  Rows keep their own batch_id and fetched_at; the merged partitions are removed; the week's control rows are
  rolled into one file.
- post-load enrichment (`coffeedata enrich-week`): after the merge, Claude's candidates from the raw rows.
- weekly build + gate, then publish: the existing `build` and `publish` steps.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import control, ingest
from .lake import SCHEMAS, Lake, new_batch_id
from .ratelimit import SHOPIFY_RPS

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


CONNECTORS: dict[str, Connector] = {
    c.name: c
    for c in (
        Connector("shopify", 0, 2, rps=SHOPIFY_RPS),
        Connector("squarespace", 1, 2),
        Connector("woocommerce", 2, 2),
        Connector("square_online", 3, 2),
        Connector("generic", 4, 2),
    )
}
MERGE_WEEKDAY, MERGE_HOUR = 6, 6  # Sunday 06:00 UTC, after every connector's resume slot
# The rest of the week, one job each, timed by Cloud Scheduler and gated by the control table:
ENRICH_HOUR = 7  # Sunday 07:00: post-load enrichment (Claude candidates from raw), after the merge
BUILD_HOUR = 9  # Sunday 09:00: classify verdicts, dbt build + gate, then publish (cli weekly's last steps)


def week_of(day: str) -> str:
    """The week's run_date: the Monday of `day`'s ISO week. Every connector's job that week writes under it."""
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def partition(lake: Lake, connector: str, week: str) -> Path:
    return lake.root / f"raw/connector={connector}/run_date={week}"


class PartitionLake(Lake):
    """The lake, as one connector job sees it: reads the lake plus its own partition, writes only the partition."""

    def __init__(self, base: Lake, connector: str, week: str):
        super().__init__(base.url, staging=base.root if base.remote else None, tables=base.tables)
        self.part = partition(self, connector, week_of(week))

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


def _write_table(path: Path, tbl: pa.Table) -> None:
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, tmp, compression="zstd")
    tmp.replace(path)


def _read(files: list[Path], table: str) -> pa.Table:
    schema = SCHEMAS[table]
    tbls = [pq.read_table(f).select([n for n in schema.names]).cast(schema) for f in files]
    return pa.concat_tables(tbls) if tbls else schema.empty_table()


def compact(part: Path) -> dict[str, int]:
    """Fold the partition's checkpoint files into one file per table (rows kept as they are)."""
    out = {}
    for table in RAW_TABLES:
        files = _partition_files(part, table)
        if not files:
            continue
        if len(files) > 1 or files[0].parent != part:
            _write_table(part / f"{table}.parquet", _read(files, table))
            for f in files:
                if f.parent != part:
                    f.unlink()
        out[table] = pq.read_metadata(part / f"{table}.parquet").num_rows
    shutil.rmtree(part / "_parts", ignore_errors=True)
    return out


def crawl(lake: Lake, connector: str, day: str, deadline_min: float | None = None) -> dict:
    """One connector's weekly job, or its resume: fetch its roasters without an ok fetch this week (parked stores
    continue from their blocked page), then compact. Extract and load only. A job_manifests row marks its start
    and its end: an attempt with only the first one died (killed or timed out)."""
    c = CONNECTORS[connector]
    week = week_of(day)
    plake = PartitionLake(lake, connector, week)
    compact(plake.part)  # a killed attempt's checkpoints, before deciding what is left
    states, _ = ingest.select_states(plake, week)
    states = [s for s in states if s.roaster.adapter == connector]
    todo_ids = {s.roaster.roaster_id for s in states}
    job = control.start(lake, "crawl", week, connector, len(states))
    out = (
        ingest.run(
            plake,
            states,
            week,
            workers=c.workers,
            mode="full",
            shopify_rps=c.rps or SHOPIFY_RPS,
            platform_rps={connector: c.rps} if c.rps else None,  # its own shared budget, from config
            deadline_s=60 * (deadline_min or c.deadline_min),
            resume={k: v for k, v in ingest.load_parked(plake, week).items() if k in todo_ids},
        )
        if states
        else {"roasters": 0, "by_adapter": {}, "skipped_deadline": 0, "deferred": {}}
    )
    rows = compact(plake.part)
    errors = sum(a.get("errors", 0) for a in out["by_adapter"].values())
    tried = sum(a.get("roasters", 0) for a in out["by_adapter"].values())
    detail = {"rows": rows, "by_adapter": out.get("by_adapter"), "deferred": out.get("deferred"),
              "shopify_limiter": out.get("shopify_limiter") if c.rps else None}  # fmt: skip
    end = control.finish(
        lake, job, "deadline" if out.get("skipped_deadline") else "done",
        ok=tried - errors, errors=errors, parked=(out.get("deferred") or {}).get("page_parked", 0),
        skipped=out.get("skipped_deadline", 0), requests=out.get("requests"), http_429=out.get("http_429"),
        http_430=out.get("http_430"), first_contact=out.get("first_contact"),
        detail=detail,
    )  # fmt: skip
    lake.publish(f"crawl {connector} {week}")
    return {"connector": connector, "run_date": week, "todo": len(states), **detail,
            "status": end["status"], "skipped_deadline": out.get("skipped_deadline", 0)}  # fmt: skip


def merge(lake: Lake, day: str) -> dict:
    """Union every connector's partition for the week into the lake's own tables, one file per table. Which
    connectors landed comes from the control table (job_manifests), not from the other jobs."""
    week = week_of(day)
    job = control.start(lake, "merge", week)
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
    for table in RAW_TABLES:
        tbl = _read([f for p in present.values() for f in _partition_files(p, table)], table)
        if table == "run_manifests" and missing:
            # A connector whose job never landed still belongs to the week's expected set: its roasters show up in
            # run_completeness as not attempted, instead of the run looking complete without them.
            states, _ = ingest.select_states(lake, week)
            gone = [s for s in states if s.roaster.adapter in missing]
            rows = ingest.manifest_rows(
                [s.roaster for s in gone], date.fromisoformat(week), f"missing-{batch}",
                {s.roaster.roaster_id: s.status for s in gone}, "full", None,
            )  # fmt: skip
            tbl = pa.concat_tables([tbl, pa.Table.from_pylist(rows, schema=SCHEMAS[table])])
        # A re-run merge (a late connector) adds only what the lake doesn't have yet.
        con.register("new", tbl)
        key = {"run_manifests": "manifest_id", "fetches": "batch_id", "snapshots": "batch_id"}.get(table)
        if key:
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
    for p in present.values():  # the week's data now lives in the lake's tables; resumes read it from there
        shutil.rmtree(p)
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


def enrich_week(lake: Lake, day: str, limit: int = 2000, submit: bool = False) -> dict:
    """The post-load enrichment job: after the week's merge (it reads that from the control table), Claude's
    candidates come from the raw rows (enrich.candidates, the build's own rules). It spends only when asked to
    (submit: `--submit` on the scheduled job) and it has credentials (enrich.collect + enrich.run); otherwise it
    only counts them."""
    from . import enrich, extract

    week = week_of(day)
    if not any(r["job"] == "merge" and r["status"] == "done" for r in control.rows(lake, week)):
        return {"run_date": week, "status": "waiting: no merge this week yet"}
    job = control.start(lake, "enrich", week)
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
