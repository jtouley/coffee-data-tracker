"""Per-connector ingestion pipelines (prototype, Design C): small scheduled jobs that land raw results, and a weekly
merge that hands dbt the lake's usual run.

    raw/connector=<c>/run_date=<d>/shard=<s>/data.parquet   one file per partition: every row a job wrote
    control/manifests/run_date=<d>/job=<id>.parquet         one file per job: each store's outcome (the control table)
    control/rollup/week=<d>.parquet                         the week's manifests, rolled into one file by the merge

Jobs (config/connectors.toml): one per connector, weekly, except Shopify, which runs a seventh of its stores a day
(sha256(roaster_id) mod 7 == weekday) at a rate under its IP-wide limit; a daily retry sweep over the stores the
control table has parked, in waves hours apart that honor Retry-After; and a daily HTML-fallback stub for stores
that refused in several jobs. A job writes through ingest.run unchanged (per-host backoff, deferred rounds):
RawSink stands in for the Lake, so the rows are exactly the lake's rows, only parked under raw/. Parts are
checkpointed every `chunk` roasters; a killed job re-run on the same day skips what it already fetched, and its
parts are compacted into the one data.parquet when it ends.

The weekly merge picks, per roaster, the latest successful fetch of the last 7 days (else its latest failure, so
it still counts as attempted), rewrites run_date and batch_id to the weekly run's, and writes run_manifests,
fetches, snapshots, bodies and enrichments to the lake as one ingest would. dbt reads nothing under raw/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from . import ingest, registry
from .lake import SCHEMAS, Lake, new_batch_id
from .models import Roaster

log = logging.getLogger(__name__)

CONFIG = Path("config/connectors.toml")
# Contracts, versioned in each file's Parquet metadata and pinned by tests/test_pipeline_contracts.py. Raw: one row per
# lake row a job produced; `kind` is its lake table (RAW_KINDS) and `row` its JSON in that table's schema (lake.SCHEMAS).
RAW_CONTRACT, CONTROL_CONTRACT = "raw/v1", "control/v1"
RAW_KINDS = ("run_manifests", "fetches", "snapshots", "bodies")  # ELT: never enrichments
OUTCOMES = ("ok", "error", "parked", "refused_attempt", "refused", "html_stub")
RAW = pa.schema(
    [("kind", pa.string()), ("roaster_id", pa.string()), ("row", pa.string())],
    metadata={"contract": RAW_CONTRACT},
)
CONTROL = pa.schema(
    [
        ("job_id", pa.string()),
        ("job", pa.string()),  # connector, sweep or html_fallback
        ("connector", pa.string()),
        ("shard", pa.string()),
        ("run_date", pa.date32()),
        ("ended_at", pa.timestamp("s", tz="UTC")),
        ("roaster_id", pa.string()),
        # ok | error (permanent) | parked (transient: retry) | refused_attempt | refused (routed) | html_stub
        ("outcome", pa.string()),
        ("error", pa.string()),
        ("fetched_at", pa.timestamp("s", tz="UTC")),
        ("retry_at", pa.timestamp("s", tz="UTC")),
        ("requests", pa.int32()),
        ("rate_limited", pa.int32()),
    ],
    metadata={"contract": CONTROL_CONTRACT},
)
_WAIT = re.compile(r"wait (\d+)s")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(Path(path).read_text())


def raw_root() -> Path:
    return Path(os.environ.get("COFFEEDATA_RAW", "build/raw"))


def shard_of(roaster_id: str, shards: int = 7) -> int:
    return int(hashlib.sha256(roaster_id.encode()).hexdigest(), 16) % shards


def _write(path: Path, schema: pa.Schema, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), tmp, compression="zstd")
    tmp.rename(path)


def _revive(table: str, row: dict) -> dict:
    """A lake row read back from raw JSON: dates and timestamps as Python values again."""
    out = {}
    for f in SCHEMAS[table]:
        v = row.get(f.name)
        if isinstance(v, str) and pa.types.is_date32(f.type):
            v = date.fromisoformat(v)
        elif isinstance(v, str) and pa.types.is_timestamp(f.type):
            v = datetime.fromisoformat(v)
        out[f.name] = v
    return out


class RawSink:
    """The Lake as ingest.run sees it, writing to one raw partition: rows are buffered per checkpoint (publish) into a
    part file, read the real lake for known bodies and enrichments, and seal() compacts the parts into data.parquet."""

    def __init__(self, lake: Lake, root: Path, connector: str, run_date: str, shard: str):
        self.lake = lake
        self.root = root  # ingest.run parks a store blocked mid-catalog under root/parked/ (outside raw/)
        self.dir = root / f"raw/connector={connector}/run_date={run_date}/shard={shard}"
        self._pending: list[dict] = []
        self._lock = threading.Lock()

    # --- what ingest.run calls
    def write_partition(self, table: str, run_date: str, batch_id: str, rows: list[dict]) -> None:
        with self._lock:
            self._pending += [
                {"kind": table, "roaster_id": r.get("roaster_id"), "row": json.dumps(r, default=str)}
                for r in rows
            ]

    def publish(self, message: str, **_: object) -> int:
        with self._lock:
            rows, self._pending = self._pending, []
        if rows:
            _write(self.dir / "_parts" / f"part-{new_batch_id()}.parquet", RAW, rows)
        return 0

    def connect(self, *a, **kw):
        return self.lake.connect(*a, **kw)

    def scan(self, table: str) -> str:
        return self.lake.scan(table)

    # --- the pipeline's own
    def files(self) -> list[Path]:
        return sorted(self.dir.glob("data.parquet")) + sorted(self.dir.glob("_parts/*.parquet"))

    def rows(self) -> list[dict]:
        return [r for p in self.files() for r in pq.read_table(p).to_pylist()]

    def fetched_ok(self) -> set[str]:
        return {
            r["roaster_id"]
            for r in self.rows()
            if r["kind"] == "fetches" and json.loads(r["row"])["status"] == "ok"
        }

    def seal(self) -> None:
        """One file per partition: the parts (and a data.parquet from an earlier run of this job) become one."""
        files = self.files()
        if len(files) <= 1 and (not files or files[0].name == "data.parquet"):
            return
        rows = self.rows()
        _write(self.dir / "data.parquet.new", RAW, rows)
        (self.dir / "data.parquet.new").replace(self.dir / "data.parquet")
        shutil.rmtree(self.dir / "_parts", ignore_errors=True)


@dataclass
class Outcomes:
    """The last attempt per roaster in one job (ingest.run's on_result), turned into control rows."""

    job_id: str
    job: str
    connector: str
    shard: str
    run_date: str
    last: dict[str, ingest.Result] = field(default_factory=dict)
    # roaster -> (429s, catalog pages, every attempt failed deferrably, requests) over the job's attempts
    seen: dict[str, tuple[int, int, bool, int]] = field(default_factory=dict)

    def __call__(self, res: ingest.Result) -> None:
        rid = res.roaster.roaster_id
        n429, pages, failing, reqs = self.seen.get(rid, (0, 0, True, 0))
        self.seen[rid] = (
            n429 + res.stats.rate_limited,
            pages + res.stats.pages,
            failing and bool(res.error) and res.deferrable,
            reqs + res.stats.requests,
        )
        self.last[rid] = res

    def rows(self, min_retry_s: float = 60.0) -> list[dict]:
        """A refusal: every attempt this job failed on a block or an unanswered robots.txt, at least one with a 429,
        and no catalog page came back (PR #62's own "refused:" label, or this, since a robots.txt 429 is remembered
        for 15 minutes and the deferred rounds then fail without asking)."""
        now = utcnow()
        out = []
        for rid, res in self.last.items():
            err = res.error or ""
            n429, pages, failing, reqs = self.seen[rid]
            refused = err.startswith("refused") or (failing and n429 > 0 and pages == 0)
            outcome = (
                "ok"
                if not res.error
                else "refused_attempt"
                if refused
                else "parked"
                if res.deferrable or err.startswith("parked")
                else "error"
            )
            wait = max([min_retry_s] + [float(w) for w in _WAIT.findall(err)])
            out.append(
                {
                    "job_id": self.job_id,
                    "job": self.job,
                    "connector": self.connector,
                    "shard": self.shard,
                    "run_date": date.fromisoformat(self.run_date),
                    "ended_at": now,
                    "roaster_id": rid,
                    "outcome": outcome,
                    "error": res.error,
                    "fetched_at": res.fetched_at,
                    "retry_at": now + timedelta(seconds=wait)
                    if outcome in ("parked", "refused_attempt")
                    else None,
                    "requests": reqs,
                    "rate_limited": n429,
                }
            )
        return out


def write_control(root: Path, run_date: str, job_id: str, rows: list[dict]) -> None:
    if rows:
        _write(root / f"control/manifests/run_date={run_date}/job={job_id}.parquet", CONTROL, rows)


def control_rows(root: Path, since: datetime) -> list[dict]:
    files = sorted((root / "control").glob("manifests/*/*.parquet")) + sorted(
        (root / "control").glob("rollup/*.parquet")
    )
    rows = [r for p in files for r in pq.read_table(p).to_pylist()]
    return sorted((r for r in rows if r["ended_at"] >= since), key=lambda r: r["ended_at"])


@dataclass
class StoreState:
    outcome: str | None = None  # the latest API attempt's
    retry_at: datetime | None = None
    last_ok: datetime | None = None
    refusals: list[datetime] = field(default_factory=list)  # refused jobs since the last success
    connector: str = ""

    def refused(self, cfg: dict) -> bool:
        s = cfg["sweep"]
        r = self.refusals
        return len(r) >= s["refuse_after_jobs"] and (r[-1] - r[0]) >= timedelta(hours=s["refuse_gap_h"])


def store_state(root: Path, now: datetime, days: int = 7) -> dict[str, StoreState]:
    """The control table: each store's state from the last `days` of job manifests."""
    state: dict[str, StoreState] = defaultdict(StoreState)
    for r in control_rows(root, now - timedelta(days=days)):
        if r["outcome"] == "html_stub":
            continue
        s = state[r["roaster_id"]]
        s.outcome, s.retry_at, s.connector = r["outcome"], r["retry_at"], r["connector"]
        if r["outcome"] == "ok":
            s.last_ok, s.refusals = r["fetched_at"], []
        elif r["outcome"] in ("refused_attempt", "refused"):
            s.refusals.append(r["ended_at"])
    return dict(state)


def _budgets(cfg: dict) -> tuple[float, dict[str, tuple[float, int]]]:
    conns = cfg["connectors"]
    limits = {n: (c["rps"], c["concurrency"]) for n, c in conns.items() if "rps" in c and n != "shopify"}
    return conns["shopify"].get("rps", ingest.SHOPIFY_RPS), limits


def roster(lake: Lake, run_date: str) -> list[registry.RoasterState]:
    """Every roaster a full weekly ingest would fetch."""
    return ingest.select_states(lake, run_date)[0]


def _rid(s) -> str:
    return (s.roaster if hasattr(s, "roaster") else s).roaster_id


def parked_progress(root: Path, since: date) -> dict[str, dict]:
    """Stores ingest.run parked mid-catalog (shopify.PageBlocked) since `since`: their blocked page and the products
    before it, the latest per store, so the sweep resumes each from that page."""
    out: dict[str, dict] = {}
    for d in sorted((root / "parked").glob("run_date=*")):
        rd = d.name.removeprefix("run_date=")
        if date.fromisoformat(rd) >= since:
            out |= ingest.load_parked(SimpleNamespace(root=root), rd)
    return out


def _fetch(
    lake: Lake,
    root: Path,
    cfg: dict,
    job: str,
    connector: str,
    shard: str,
    run_date: str,
    todo: list,
    job_id: str,
    workers: int,
    chunk: int,
    resume: dict[str, dict] | None = None,
) -> tuple[dict, Outcomes]:
    sink = RawSink(lake, root, connector, run_date, shard)
    done = sink.fetched_ok()  # a re-run of a killed job: what its parts already hold
    todo = [s for s in todo if _rid(s) not in done]
    outcomes = Outcomes(job_id, job, connector, shard, run_date)
    rps, limits = _budgets(cfg)
    out = (
        ingest.run(
            sink,
            todo,
            run_date,
            workers=workers,
            mode="full",
            shopify_rps=rps,
            limits=limits,
            chunk=chunk,
            on_result=outcomes,
            enrich_rules=False,  # ELT: extract and load only; run_enrich works on what has landed
            # the sweep retries known blockers: their 429s are expected and never a platform-wide signal
            quiet_hosts=[(s.roaster if hasattr(s, "roaster") else s).host for s in todo]
            if job == "sweep"
            else (),
            resume={k: v for k, v in (resume or {}).items() if k in {_rid(s) for s in todo}} or None,
        )
        if todo
        else {}
    )
    sink.seal()
    return out | {"resumed_skipped": len(done), "todo": len(todo)}, outcomes


def run_connector(
    lake: Lake,
    connector: str,
    run_date: str,
    cfg: dict | None = None,
    root: Path | None = None,
    shard_k: int | None = None,
) -> dict:
    """One connector's scheduled job: its weekly run, or today's Shopify shard (shard_k: another one, by hand)."""
    cfg, root = cfg or load_config(), root or raw_root()
    c = cfg["connectors"][connector]
    states = [s for s in roster(lake, run_date) if s.roaster.adapter == connector]
    shard = "all"
    if c["schedule"] == "daily_shard":
        k = date.fromisoformat(run_date).weekday() % c["shards"] if shard_k is None else shard_k
        shard = str(k)
        states = [s for s in states if shard_of(s.roaster.roaster_id, c["shards"]) == k]
    job_id = f"{connector}-{run_date}-{shard}-{new_batch_id()}"
    out, outcomes = _fetch(
        lake,
        root,
        cfg,
        connector,
        connector,
        shard,
        run_date,
        states,
        job_id,
        c.get("workers", 32),
        c.get("chunk", ingest.CHUNK),
    )
    write_control(root, run_date, job_id, outcomes.rows())
    return out | {"job_id": job_id, "shard": shard, "stores": len(states)}


def run_sweep(lake: Lake, run_date: str, cfg: dict | None = None, root: Path | None = None) -> dict:
    """The daily retry sweep: parked stores, in waves (cfg sweep.waves_min after the sweep starts), each store only
    once its Retry-After has passed. A store refused in enough jobs, hours apart, is routed to the HTML fallback."""
    cfg, root = cfg or load_config(), root or raw_root()
    sc = cfg["sweep"]
    by_id = {s.roaster.roaster_id: s for s in roster(lake, run_date)}
    state = store_state(root, utcnow())
    progress = parked_progress(root, (utcnow() - timedelta(days=7)).date())
    pending = {
        rid
        for rid, s in state.items()
        if s.outcome in ("parked", "refused_attempt") and not s.refused(cfg) and rid in by_id
    }
    candidates = len(pending)
    t0 = time.monotonic()
    limit = t0 + sc["deadline_min"] * 60 - 600  # leave 10 minutes to seal and write the control table
    waves = []
    for k, off in enumerate(sc["waves_min"]):
        if not pending or time.monotonic() >= limit:
            break
        if (wait := t0 + off * 60 - time.monotonic()) > 0:
            time.sleep(wait)
        now = utcnow()
        due = sorted(rid for rid in pending if (state[rid].retry_at or now) <= now)
        if not due:
            continue
        job_id = f"sweep-{run_date}-w{k}-{new_batch_id()}"
        groups: dict[str, list] = defaultdict(list)
        for rid in due:
            groups[by_id[rid].roaster.adapter].append(by_id[rid])
        results: dict[str, tuple[dict, Outcomes]] = {}

        def go(conn: str, todo: list, job_id: str = job_id, results: dict = results) -> None:
            results[conn] = _fetch(
                lake, root, cfg, "sweep", conn, "retry", run_date, todo, job_id, 16, 25, progress
            )

        threads = [threading.Thread(target=go, args=(c, t)) for c, t in groups.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = [r for _, o in results.values() for r in o.rows()]
        nxt = sc["waves_min"][k + 1] - off if k + 1 < len(sc["waves_min"]) else 0
        routed = 0
        for r in rows:
            s = state[r["roaster_id"]]
            if r["outcome"] == "refused_attempt":
                s.refusals.append(r["ended_at"])
            if r["outcome"] in (
                "parked",
                "refused_attempt",
            ):  # Retry-After, or the next wave, whichever later
                s.retry_at = max(r["retry_at"], r["ended_at"] + timedelta(minutes=nxt))
                r["retry_at"] = s.retry_at
            if r["outcome"] in ("ok", "error"):
                pending.discard(r["roaster_id"])
            elif s.refused(cfg):
                pending.discard(r["roaster_id"])
                r["outcome"], routed = "refused", routed + 1
        write_control(root, run_date, job_id, rows)
        waves.append(
            {
                "wave": k,
                "due": len(due),
                "ok": sum(r["outcome"] == "ok" for r in rows),
                "routed_refused": routed,
                "requests": sum(r["requests"] or 0 for r in rows),
                "rate_limited": sum(r["rate_limited"] or 0 for r in rows),
            }
        )
    return {"candidates": candidates, "waves": waves, "still_parked": len(pending)}


# --- the secondary crawler for stores whose API refuses us ------------------------------------------------------


def html_crawl(client: object, roaster: Roaster) -> list:
    """Hook for a crawler of the store's HTML pages (sitemap + product pages), for stores whose catalog API refuses
    this IP. Not built: the prototype only routes to it."""
    raise NotImplementedError("html fallback crawler: stub")


FALLBACKS = {"refused": html_crawl}


def run_html_fallback(lake: Lake, run_date: str, cfg: dict | None = None, root: Path | None = None) -> dict:
    """Route every refused store to its fallback crawler; record the routing in the control table."""
    cfg, root = cfg or load_config(), root or raw_root()
    now = utcnow()
    by_id = {s.roaster.roaster_id: s.roaster for s in roster(lake, run_date)}
    refused = sorted(rid for rid, s in store_state(root, now).items() if s.refused(cfg) and rid in by_id)
    job_id = f"html-{run_date}-{new_batch_id()}"
    rows = []
    for rid in refused:
        try:
            FALLBACKS["refused"](None, by_id[rid])
            outcome, err = "ok", None
        except NotImplementedError as e:
            outcome, err = "html_stub", str(e)
        rows.append(
            {
                "job_id": job_id,
                "job": "html_fallback",
                "connector": "html",
                "shard": "refused",
                "run_date": date.fromisoformat(run_date),
                "ended_at": now,
                "roaster_id": rid,
                "outcome": outcome,
                "error": err,
                "fetched_at": None,
                "retry_at": None,
                "requests": 0,
                "rate_limited": 0,
            }
        )
    write_control(root, run_date, job_id, rows)
    return {"routed": len(refused)}


# --- the weekly merge ----------------------------------------------------------------------------------------------


def run_merge(lake: Lake, run_date: str, cfg: dict | None = None, root: Path | None = None) -> dict:
    """The weekly run in the lake's usual shape, from the raw partitions of the last window_days."""
    cfg, root = cfg or load_config(), root or raw_root()
    now = utcnow()
    since = now - timedelta(days=cfg["merge"]["window_days"])
    files = [
        p
        for p in sorted(root.glob("raw/connector=*/run_date=*/shard=*/**/*.parquet"))
        if date.fromisoformat(p.as_posix().split("run_date=")[1][:10]) >= since.date()
    ]
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for p in files:
        for r in pq.read_table(p).to_pylist():
            by_kind[r["kind"]].append(_revive(r["kind"], json.loads(r["row"])))
    best: dict[str, dict] = {}
    for f in sorted(by_kind["fetches"], key=lambda f: f["fetched_at"]):
        if f["fetched_at"] < since:
            continue
        cur = best.get(f["roaster_id"])
        if cur is None or f["status"] == "ok" or cur["status"] != "ok":
            best[f["roaster_id"]] = f
    expected = roster(lake, run_date)
    rd, batch, manifest = date.fromisoformat(run_date), new_batch_id(), new_batch_id()
    keep = {(f["roaster_id"], f["batch_id"]) for f in best.values() if f["status"] == "ok"}
    snaps = [s for s in by_kind["snapshots"] if (s["roaster_id"], s["batch_id"]) in keep]
    con = lake.connect()
    known = {r[0] for r in con.execute(f"select distinct body_hash from {lake.scan('bodies')}").fetchall()}
    want = {s["body_hash"] for s in snaps if s["body_hash"]} - known
    bodies = list(
        {
            b["body_hash"]: b | {"first_seen_run": rd} for b in by_kind["bodies"] if b["body_hash"] in want
        }.values()
    )
    statuses = {s.roaster.roaster_id: s.status for s in expected}
    lake.write_partition(
        "run_manifests",
        run_date,
        manifest,
        ingest.manifest_rows(
            [s.roaster for s in expected], rd, manifest, statuses, "full", "pipelines-merge"
        ),
    )
    lake.write_partition(
        "fetches", run_date, batch, [f | {"run_date": rd, "batch_id": batch} for f in best.values()]
    )
    lake.write_partition(
        "snapshots", run_date, batch, [s | {"run_date": rd, "batch_id": batch} for s in snaps]
    )
    lake.write_partition("bodies", run_date, batch, bodies)
    # Loaded only: enrichment (run_enrich), the build and its gate (`coffeedata build`) and the upload
    # (`coffeedata publish`) are their own scheduled jobs, each reading what this one wrote.
    ids = {s.roaster.roaster_id for s in expected}
    rolled = rollup(root, run_date)
    return {
        "expected": len(ids),
        "ok": sum(f["status"] == "ok" for f in best.values() if f["roaster_id"] in ids),
        "attempted": len(ids & best.keys()),
        "never_fetched": len(ids - best.keys()),
        "snapshots": len(snaps),
        "bodies": len(bodies),
        "raw_files_read": len(files),
        "manifests_rolled": rolled,
    }


# --- post-load enrichment -------------------------------------------------------------------------------------------

# Coffees in the lake without a rules enrichment for their (roaster, product, body): the same key ingest used.
RULES_TODO_SQL = """
select s.* exclude (run_date, batch_id, is_coffee, payload_hash), s.payload_hash, b.body_text
from {snapshots} s
left join (select body_hash, any_value(body_text) as body_text from {bodies} group by 1) b using (body_hash)
anti join (select distinct roaster_id, product_id, body_hash from {enrichments} where method = 'rules') e
    using (roaster_id, product_id, body_hash)
where s.is_coffee and s.roaster_id is not null
qualify row_number() over (partition by s.roaster_id, s.product_id, s.body_hash order by s.fetched_at) = 1
"""


def run_enrich(lake: Lake, run_date: str, cfg: dict | None = None, root: Path | None = None) -> dict:
    """Post-load enrichment, weekly after the merge: rules for every coffee that landed without them, then Claude's
    candidates (enrich.pending: rules rows that need_llm, joined to their bodies), collected and submitted as today.
    Weekly because the lake gets the week's snapshots and bodies only at the merge (raw/ is not a lake table), and
    one Batches submission a week is what the build needs."""
    from . import enrich, extract
    from .models import CatalogItem

    cur = lake.connect().execute(
        RULES_TODO_SQL.format(**{t: lake.scan(t) for t in ("snapshots", "bodies", "enrichments")})
    )
    cols = [d[0] for d in cur.description]
    rows = []
    for row in cur.fetchall():
        r = dict(zip(cols, row, strict=True))
        item = CatalogItem.model_validate(
            {k: v for k, v in r.items() if v is not None or k == "published_at"}
        )
        item.body_text = r.get("body_text") or ""
        rows.append(ingest.rules_row(item, r["roaster_id"], r["fetched_at"]))
    batch = new_batch_id()
    lake.write_partition("enrichments", run_date, batch, rows)
    out: dict = {"rules_rows": len(rows), "llm_candidates": len(enrich.pending(lake, 100_000))}
    if extract.llm_enabled():
        out |= {"collect": enrich.collect(lake, run_date), "enrich": enrich.run(lake, run_date=run_date)}
    return out


def rollup(root: Path, run_date: str) -> int:
    """The week's job manifests become one file; the daily ones are removed."""
    files = sorted((root / "control").glob("manifests/*/*.parquet"))
    if not files:
        return 0
    rows = [r for p in files for r in pq.read_table(p).to_pylist()]
    _write(root / f"control/rollup/week={run_date}.parquet", CONTROL, rows)
    shutil.rmtree(root / "control/manifests")
    return len(files)
