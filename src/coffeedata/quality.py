"""Builds, releases and serving: what each dbt build saw and produced, which one customers get, and rollback.

Every build appends one `builds` file: a row per dbt node (manifest checksum, test status and failures, row count),
a row per catalog metric (mart_build_profile, which the next build's volume test compares against), its verdict,
and its manifest.json and run_results. Every build, passing or not, also writes an immutable release: the tables
customers read (RELEASE_TABLES) under releases/run_date=.../build=.../. Only a passing release is served:
`serve` copies it to marts/ (what the app, MCP and dataset readers use) and records which release that is in
marts/release.parquet. A failed build leaves the served release as it was, which is the automatic rollback;
`rollback` serves an earlier release by hand. `build-diff` compares any two builds down to individual coffees.
Raw lake files are never touched here, and no release is ever edited.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .lake import Lake, new_batch_id, no_files

log = logging.getLogger(__name__)

# What customers read: the catalog marts (dataset, MCP, app, site) and the public tables. Shots and bags are
# personal and not part of a weekly release.
SERVED_MARTS = ("dim_coffee", "dim_roaster", "mart_market", "fct_price_event")
RELEASE_TABLES = (
    *SERVED_MARTS,
    "mart_new_arrivals",
    "public_coffee",
    "public_roaster",
    "public_price_event",
    "public_market_weekly",
)
OVERRIDE_ACTIONS = ("accept", "reject", "use_batch")


def _git_sha() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return r.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def record_build(
    lake: Lake,
    db: Path,
    run_date: str,
    manifest_path: Path,
    results: list[dict],
    passed: bool,
    build_id: str | None = None,
) -> bool:
    """Append this build's release and record to the lake: passing or not, nothing is lost. `results` holds one
    run_results.json per dbt step (models, then tests). Returns the recorded verdict: `passed`, and only if every
    release table was written, since a release missing a table can't be served."""
    import duckdb

    build_id = build_id or new_batch_id()  # the one the tables carry as release_build_id, when given
    manifest = _read_json(manifest_path)
    nodes = manifest.get("nodes") or {}
    base = {
        "build_id": build_id,
        "built_at": datetime.now(UTC).replace(microsecond=0),
        "run_date": date.fromisoformat(run_date),
        "git_sha": _git_sha(),
        "invocation_id": ",".join(
            i for r in results if (i := (r.get("metadata") or {}).get("invocation_id"))
        ),
    }
    con = duckdb.connect(str(db), read_only=True) if db.exists() else None
    written = set()
    for t in RELEASE_TABLES if con else ():
        try:
            lake.write_release(t, run_date, build_id, con, f"select * from {t}")
            written.add(t)
        except (
            duckdb.Error
        ) as e:  # a model that failed to build, or a failed write: the release is incomplete
            log.warning("release %s has no %s: %s", build_id, t, e)
    passed = passed and written == set(RELEASE_TABLES)

    def count(name: str | None) -> int | None:
        if con is None or not name:
            return None
        try:
            row = con.execute(f'select count(*) from "{name}"').fetchone()
        except duckdb.Error:
            return None
        return row[0] if row else None

    # The gate's verdict, explicitly: a build whose dbt step crashed has no failing node to show for it.
    rows = [base | {"kind": "build", "unique_id": "build", "status": "pass" if passed else "fail"}]
    for r in (r for step in results for r in step.get("results") or []):
        node = nodes.get(r.get("unique_id")) or {}
        kind = node.get("resource_type")
        rows.append(
            base
            | {
                "kind": "node",
                "unique_id": r.get("unique_id"),
                "resource_type": kind,
                "checksum": (node.get("checksum") or {}).get("checksum"),
                "status": r.get("status"),
                "failures": r.get("failures"),
                "rows": count(node.get("alias") or node.get("name")) if kind in ("model", "seed") else None,
                "execution_s": r.get("execution_time"),
            }
        )
    try:
        metrics = con.execute("select metric, value from mart_build_profile").fetchall() if con else []
    except duckdb.Error:
        metrics = []
    rows += [base | {"kind": "metric", "unique_id": m, "value": v} for m, v in metrics]
    if manifest:
        rows.append(
            base | {"kind": "artifact", "unique_id": "manifest.json", "payload": json.dumps(manifest)}
        )
    for step, r in zip(("build", "test"), results, strict=False):
        if r:
            rows.append(
                base | {"kind": "artifact", "unique_id": f"run_results.{step}.json", "payload": json.dumps(r)}
            )
    lake.write("builds", f"builds/run_date={run_date}/build={build_id}.parquet", rows)
    if con:
        con.close()
    return passed


def add_override(
    lake: Lake, roaster_id: str, run_date: str, action: str, reason: str, batch_id: str | None
) -> Path:
    if action not in OVERRIDE_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(OVERRIDE_ACTIONS)}")
    if (action == "use_batch") != bool(batch_id):
        raise ValueError("use_batch needs --batch, and only use_batch takes one")
    if not reason.strip():
        raise ValueError("say why (--reason): overrides are the audit trail for changed history")
    if batch_id:  # pin only a batch that exists for this roaster and week, and succeeded
        ok = (
            lake.connect()
            .execute(
                f"select bool_or(status = 'ok') from {lake.scan('fetches')} "
                "where roaster_id = ? and run_date = ? and batch_id = ?",
                [roaster_id, date.fromisoformat(run_date), batch_id],
            )
            .fetchone()
        )
        if not ok or ok[0] is None:
            raise ValueError(f"no fetch of {roaster_id} in batch {batch_id} for {run_date}")
        if not ok[0]:
            raise ValueError(f"batch {batch_id} failed for {roaster_id}: only a successful fetch can count")
    # added_at decides which override wins, at one-second resolution: always after the last one for this
    # roaster-week, so two given in the same second still resolve in the order they were given.
    added_at = datetime.now(UTC).replace(microsecond=0)
    try:
        last = (
            lake.connect()
            .execute(
                f"select max(added_at) from {lake.scan('run_overrides')} where roaster_id = ? and run_date = ?",
                [roaster_id, date.fromisoformat(run_date)],
            )
            .fetchone()
        )
    except Exception as e:  # noqa: BLE001 - duckdb.IOException; duckdb is imported lazily
        if not no_files(e):  # the first override: no run_overrides/ files anywhere yet
            raise
        last = None
    if last and last[0] is not None and last[0] >= added_at:
        added_at = last[0] + timedelta(seconds=1)
    row = {
        "added_at": added_at,
        "roaster_id": roaster_id,
        "run_date": date.fromisoformat(run_date),
        "action": action,
        "batch_id": batch_id,
        "reason": reason.strip(),
    }
    rel = f"run_overrides/override_id={new_batch_id()}.parquet"
    path = lake.write("run_overrides", rel, [row])
    assert path is not None
    if (
        lake.remote
    ):  # the build reads the remote lake: send just this file (a full publish would also send marts/)
        from huggingface_hub import HfApi

        repo = lake.url.removeprefix("hf://datasets/").split("@")[0]
        HfApi(token=os.environ.get("HF_TOKEN")).upload_file(
            path_or_fileobj=str(path), path_in_repo=rel, repo_id=repo, repo_type="dataset",
            commit_message=f"override: {action} {roaster_id} {run_date}",
        )  # fmt: skip
        path.unlink()  # published, like any staged file after publish
    return path


def release_files(lake: Lake, build_id: str) -> dict[str, str]:
    """A release's tables: staged locally until published, then read from the remote lake."""
    local = {p.stem: str(p) for p in lake.root.glob(f"releases/*/build={build_id}/*.parquet")}
    if local or not lake.remote:
        return local
    return {Path(f).stem: f"{lake.url}/{f}" for f in _remote_releases(lake.url) if f"/build={build_id}/" in f}


@functools.lru_cache(maxsize=8)
def _remote_releases(url: str) -> tuple[str, ...]:
    """Every release file in a remote lake: only the releases/ folder is listed, once per process (and per lake)."""
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import RepoFile

    repo = url.removeprefix("hf://datasets/").split("@")[0]
    tree = HfApi(token=os.environ.get("HF_TOKEN")).list_repo_tree(
        repo, path_in_repo="releases", recursive=True, repo_type="dataset"
    )
    return tuple(f.path for f in tree if isinstance(f, RepoFile))


def _release(lake: Lake, build_id: str, table: str) -> str:
    files = release_files(lake, build_id)
    if table not in files:
        raise FileNotFoundError(f"release {build_id} has no {table}")
    return f"read_parquet('{files[table]}', hive_partitioning = false)"


def latest_run(lake: Lake) -> str | None:
    """The newest run_date with any fetch attempt in the lake."""
    row = (
        lake.connect()
        .execute(f"select max(run_date)::varchar from {lake.scan('fetches')} where roaster_id is not null")
        .fetchone()
    )
    return row[0] if row else None


def builds(lake: Lake) -> list[tuple[str, str, bool]]:
    """(build_id, run_date, passed) for every build, oldest first (builds in the same second by build_id). Passed
    is the rule of dbt's stg_builds, which the volume check reads (tests/test_quality.py checks the two agree)."""
    rows = (
        lake.connect()
        .execute(
            f"select build_id, any_value(run_date)::varchar, "
            f"bool_or(kind = 'build' and status = 'pass') and not bool_or(coalesce(status, '') in ('error', 'fail')) "
            f"from {lake.scan('builds')} where build_id is not null group by build_id order by max(built_at), build_id"
        )
        .fetchall()
    )
    return [(b, d, bool(ok)) for b, d, ok in rows]


def served(lake: Lake) -> dict | None:
    """The release customers are getting now (marts/release.parquet), if any. Only a missing file means nothing
    is served; any other failed read (a 429, a timeout) is raised, so it is never recorded as "nothing served"."""
    try:
        src = ", ".join(f"'{s}'" for s in lake.mart_sources("release"))
        cur = lake.connect().execute(f"select * from read_parquet([{src}])")
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - duckdb.IOException / HTTPException; duckdb is imported lazily
        if no_files(e) or "HTTP 404" in str(e):  # a new lake, or one from before releases
            return None
        raise
    return dict(zip(cols, row, strict=True)) if row else None


def serve(lake: Lake, build_id: str, reason: str, db: Path = Path("build/served.duckdb")) -> Path:
    """Make a release the one customers get: its catalog marts replace marts/, marts/release.parquet records it,
    and `db` gets every release table, for the public export and the site. Returns `db`."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    passed = {b: (d, ok) for b, d, ok in builds(lake)}
    if build_id not in passed:
        raise ValueError(f"no build {build_id}")
    run_date, ok = passed[build_id]
    if not ok:
        raise ValueError(f"build {build_id} failed its checks; only a passing release can be served")
    files = release_files(lake, build_id)
    missing = [t for t in RELEASE_TABLES if t not in files]
    if missing:
        raise FileNotFoundError(f"release {build_id} is missing {', '.join(missing)}")
    if db.exists():
        db.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = lake.connect(str(db))
    for t in RELEASE_TABLES:
        # hive_partitioning off: the release's path (run_date=.../build=...) must not become columns.
        con.execute(
            f"create table {t} as select * from read_parquet('{files[t]}', hive_partitioning = false)"
        )
    for t in SERVED_MARTS:
        lake.write_mart(t, con, f"select * from {t}")
    pointer = lake.root / "marts" / "release.parquet"
    row = {"build_id": build_id, "run_date": run_date, "served_at": datetime.now(UTC).replace(microsecond=0),
           "reason": reason}  # fmt: skip
    pq.write_table(pa.Table.from_pylist([row]), pointer)
    con.close()
    duckdb.connect(str(db), read_only=True).close()  # flushed and readable
    return db


def rollback(lake: Lake, to: str | None = None, reason: str = "") -> str:
    """Serve an earlier passing release: `to` ('served': the one served now), or the passing release before the one
    served now."""
    now = (served(lake) or {}).get("build_id")
    passing = [b for b, _, ok in builds(lake) if ok]
    if to == "served":  # the same release again: re-export it and rebuild the site, with no crawl
        if not now:
            raise ValueError("no release is served yet")
        to = now
    if to is None:
        earlier = passing[: passing.index(now)] if now in passing else passing[:-1]
        if not earlier:
            raise ValueError("no earlier passing release to roll back to")
        to = earlier[-1]
    serve(lake, to, reason or f"rollback from {now}")
    return to


def build_diff(lake: Lake, old: str | None = None, new: str | None = None) -> dict:
    """What changed between two builds. By default: the latest build, passing or not, against the last passing
    build before it, i.e. after a failed weekly run, what the held-back build would have published."""
    con = lake.connect()
    b = f"(select * from {lake.scan('builds')} where build_id is not null)"
    history = [(bid, ok) for bid, _, ok in builds(lake)]  # the one definition of a passing build
    if new is None:
        new = history[-1][0] if history else None
    if old is None:  # the last passing build before `new`, never a later one
        order = [bid for bid, _ in history]
        cut = order.index(new) if new in order else len(order)
        before = [bid for bid, ok in history[:cut] if ok]
        old = before[-1] if before else None
    if not (old and new):
        return {"error": "need two builds to compare", "builds": [bid for bid, _ in history]}

    def nodes(build: str) -> dict[str, tuple]:
        return {
            r[0]: r[1:]
            for r in con.execute(
                f"select unique_id, resource_type, checksum, status, rows from {b} "
                "where build_id = ? and kind = 'node'",
                [build],
            ).fetchall()
        }

    def metrics(build: str) -> dict[str, float]:
        return dict(
            con.execute(
                f"select unique_id, value from {b} where build_id = ? and kind = 'metric'", [build]
            ).fetchall()
        )

    a, z = nodes(old), nodes(new)
    code = {
        "added": sorted(k for k in z.keys() - a.keys() if z[k][0] != "test"),
        "removed": sorted(k for k in a.keys() - z.keys() if a[k][0] != "test"),
        "changed": sorted(k for k in a.keys() & z.keys() if z[k][0] != "test" and a[k][1] != z[k][1]),
    }
    tests = {
        k: f"{a[k][2] if k in a else 'new'} -> {z[k][2]}"
        for k in z
        if z[k][0] == "test" and (k not in a or a[k][2] != z[k][2])
    }
    row_counts = {
        k.split(".")[-1]: {"old": a[k][3], "new": z[k][3]}
        for k in a.keys() & z.keys()
        if a[k][0] == "model" and a[k][3] != z[k][3]
    }
    ma, mz = metrics(old), metrics(new)
    out: dict = {
        "old": old,
        "new": new,
        "new_passed": dict(history).get(new),
        "failing": sorted(k for k, v in z.items() if v[2] in ("error", "fail")),
        "code": code,
        "tests": tests,
        "row_counts": row_counts,
        "metrics": {k: {"old": ma.get(k), "new": mz.get(k)} for k in sorted(ma.keys() | mz.keys())},
    }
    try:
        c = con.execute(
            f"""
            with a as (select * from {_release(lake, old, "dim_coffee")}),
                 z as (select * from {_release(lake, new, "dim_coffee")})
            select
                count(*) filter (where a.coffee_id is null) as coffees_new,
                count(*) filter (where z.coffee_id is null) as coffees_gone,
                count(*) filter (where a.is_listed and not z.is_listed) as delisted,
                count(*) filter (where not a.is_listed and z.is_listed) as relisted,
                count(*) filter (where a.in_stock and not z.in_stock) as went_out_of_stock,
                count(*) filter (where not a.in_stock and z.in_stock) as came_back_in_stock
            from a full join z using (coffee_id)"""
        )
        out["coffees"] = dict(zip([d[0] for d in c.description], c.fetchone(), strict=True))
        out["roasters_most_changed"] = con.execute(
            f"""
            with a as (select roaster_id, count(*) filter (where is_listed) n
                       from {_release(lake, old, "dim_coffee")} group by 1),
                 z as (select roaster_id, count(*) filter (where is_listed) n
                       from {_release(lake, new, "dim_coffee")} group by 1)
            select roaster_id, coalesce(a.n, 0) as old, coalesce(z.n, 0) as new
            from a full join z using (roaster_id)
            where coalesce(a.n, 0) <> coalesce(z.n, 0)
            order by abs(coalesce(z.n, 0) - coalesce(a.n, 0)) desc, roaster_id limit 10"""
        ).fetchall()
    except Exception as e:  # noqa: BLE001 - a build that failed before dim_coffee was built has no copy
        out["coffees"] = f"no dim_coffee release for one of these builds ({type(e).__name__})"
    return out
