"""Builds, releases and serving: what each dbt build saw and produced, which one customers get, and rollback.

Every build appends one `builds` file: a row per dbt node (manifest checksum, test status and failures, row count),
a row per catalog metric (mart_build_profile, which the next build's volume test compares against), its verdict,
and its manifest.json and run_results. Every build, passing or not, also writes an immutable release: the tables
customers read (RELEASE_TABLES) under releases/run_date=.../build=.../. Only a passing release is served:
`serve` copies it to marts/ (what the app, MCP and dataset readers use) and records which release that is in
marts/release.parquet. A failed build leaves the served release as it was, which is the automatic rollback;
`rollback` serves an earlier release by hand. `build-diff` compares any two builds down to individual coffees.
Every build also records each release table's row count and content hash (`table_hash`), so two builds of the same
lake and code can be shown to have produced the same release (`build-hashes`, docs/ingestion/RUNBOOK.md §5.6).
Every serve, rollback and re-export also appends a row to serving_log (which release, when, why, who, and the one
it replaced), so the history of what customers got survives marts/release.parquet being replaced. Beside each
release, under ops/, every build also writes its ops tables (OPS_TABLES): what it saw, for the owner's command
center. They are not release tables: never served, materialized, exported, hashed or reconciled.
Raw lake files are never touched here, and no release is ever edited.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import lineage
from .lake import Lake, new_batch_id, no_files

log = logging.getLogger(__name__)

# What customers read: the catalog marts (dataset, MCP, app, site) and the public tables. Shots and bags are
# personal and not part of a weekly release.
SERVED_MARTS = ("dim_coffee", "dim_roaster", "mart_market", "fct_price_event")
# The public tables release_changes reconciles against the served release.
RECONCILED_TABLES = ("public_coffee", "public_price_event", "public_roaster", "public_market_weekly")
RELEASE_TABLES = (
    *SERVED_MARTS,
    "mart_new_arrivals",
    "public_coffee",
    "public_roaster",
    "public_price_event",
    "public_market_weekly",
)
# The build's ops tables, written beside each release under ops/ (releases/run_date=.../build=<id>/ops/<table>.parquet)
# for the owner's command center, cast to lake.SCHEMAS (contracts/internal/ops.odcs.yaml): the run's completeness per
# roaster, release_changes' rows, and every product with the build's coffee decision and its reason. Not release
# tables: release_files() never lists them, so they are never served, materialized for the site or the public
# export, hashed or reconciled. Only a record: writing them never changes a build's verdict.
OPS_TABLES = ("run_completeness", "release_changes", "product_decisions")
OVERRIDE_ACTIONS = ("accept", "reject", "use_batch")
# serving_log.action: a new release served (serve), an earlier one served by hand (rollback), or the release served
# now served again to re-export it (reexport: `rollback --to served`, or --to the served build).
SERVING_ACTIONS = ("serve", "rollback", "reexport")
# The release columns that differ between two builds of the same lake by design: which build a row came from. The
# content hash leaves them out, and so does tests/test_reproducible.py's row-for-row comparison.
PER_BUILD_COLUMNS = ("release_build_id",)
# How table_hash encodes and combines rows. A change to the encoding gets a new name, so hashes made two ways are
# never compared as if they were made one way (build-hashes reports a method mismatch instead of a difference).
HASH_METHOD = "row-md5-sum-v1"
# The coverage we publish (marketplace/LISTING.md, copied by hand into the Snowflake listing's attribute boxes),
# checked by every build against its own mart_build_profile: dbt's coverage_claims_match_build (warn, beyond
# claim_tolerance) and coverage_claims_name_metrics (fails: a claim naming no metric is a bug, not drift). Found
# from this package's checkout (the ingest image copies it there), never from the working directory.
ROOT = Path(__file__).resolve().parents[2]
LISTING = ROOT / "marketplace" / "LISTING.md"
_CLAIMS = re.compile(r"<!-- coverage-claims:(.*?)-->", re.S)
_CLAIM = re.compile(r"^([a-z][a-z0-9_]*(?::[a-z0-9_]+)?): (.+)$")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_UNREAD = object()  # serve()'s `previous` when the caller hasn't read the served release


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
    duration_s: float | None = None,
) -> bool:
    """Append this build's release and record to the lake: passing or not, nothing is lost. `results` holds one
    run_results.json per dbt step (models, then tests); `duration_s` is how long they took. Every row carries the
    build's environment: the CI run, dbt's and the key packages' versions. Returns the recorded verdict: `passed`,
    and only if every release table was written, since a release missing a table can't be served."""
    import duckdb

    build_id = build_id or new_batch_id()  # the one the tables carry as release_build_id, when given
    manifest = _read_json(manifest_path)
    nodes = manifest.get("nodes") or {}
    ci_run_id, ci_run_url = lineage.ci_run()
    dbt_version = next(
        (v for r in results if (v := (r.get("metadata") or {}).get("dbt_version"))), None
    ) or lineage.package_version("dbt-core")
    base = {
        "build_id": build_id,
        "built_at": datetime.now(UTC).replace(microsecond=0),
        "run_date": date.fromisoformat(run_date),
        "git_sha": lineage.git_sha()
        or lineage.UNKNOWN,  # the image's commit (COFFEEDATA_GIT_SHA) on Cloud Run
        "invocation_id": ",".join(
            i for r in results if (i := (r.get("metadata") or {}).get("invocation_id"))
        ),
        "ci_run_id": ci_run_id,
        "ci_run_url": ci_run_url,
        "dbt_version": dbt_version,
        "package_versions": lineage.package_versions(),
        "duration_s": None if duration_s is None else round(duration_s, 3),
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
    ops = write_ops(lake, con, run_date, build_id) if con else {}
    hashes = []  # each release table's content, for build-hashes: what a rebuild of this release must reproduce
    for t in sorted(written):
        try:
            n, h = table_hash(con, t)
        except duckdb.Error as e:  # only a record: it never changes the verdict
            log.warning("release %s: no hash for %s: %s", build_id, t, e)
            continue
        hashes.append(
            {"kind": "table_hash", "unique_id": t, "rows": n, "checksum": h, "payload": HASH_METHOD}
        )

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
    try:  # the crawl success SLO (#96): report only, built after the gate, so absent when its step failed
        metrics += con.execute("select metric, value from mart_crawl_slo_profile").fetchall() if con else []
    except duckdb.Error as e:
        log.warning("build %s: no crawl success SLO recorded: %s", build_id, e)
    rows += [base | {"kind": "metric", "unique_id": m, "value": v} for m, v in metrics]
    rows += [base | h for h in hashes]
    # Each ops table written beside the release, and its row count (a missing one is in the build's log).
    rows += [base | {"kind": "ops_table", "unique_id": t, "rows": n} for t, n in sorted(ops.items())]
    # The release notes: how many rows of each public table this build adds, removes or changes against the
    # served release, per reason (release_changes); unique_id is <table>:<change>:<reason>.
    rows += [
        base | {"kind": "release_change", "unique_id": ":".join(k), "value": float(n)}
        for k, n in release_change_counts(con)
    ]
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


def write_ops(lake: Lake, con, run_date: str, build_id: str) -> dict[str, int]:
    """Write this build's ops tables (OPS_TABLES) beside its release, each exactly its contract's columns cast to
    lake.SCHEMAS, the build's id first; returns {table: rows} for those written. One that can't be (a model that didn't build, a column
    the contract doesn't match) is logged and left out: only a record, so it never changes the verdict."""
    import pyarrow as pa

    from .lake import SCHEMAS

    out = {}
    for t in OPS_TABLES:
        schema = SCHEMAS[t]
        try:
            cols = ", ".join(f'"{c}"' for c in schema.names[1:])  # build_id is this build's, not the model's
            table = con.sql(f"select {cols} from {t}").to_arrow_table()
            table = table.add_column(0, "build_id", pa.array([build_id] * table.num_rows, pa.string()))
            table = table.cast(schema)
            lake.write_ops(t, run_date, build_id, table)
        except Exception as e:  # noqa: BLE001 - duckdb.Error (no model), pyarrow's cast errors, an existing file
            log.warning("release %s: ops table %s not written: %s", build_id, t, e)
            continue
        out[t] = table.num_rows
    return out


def stage_previous_release(lake: Lake, build_id: str, dest: Path, attempts: int = 3) -> tuple[dict, str]:
    """Copy the served release's public tables to `dest` for release_changes, and list each file's columns: the
    dbt vars previous_release ({table: {"path", "columns"}}) and previous_release_status. A failed read (a 429 or a
    timeout on a remote lake) is retried, then logged and reported as status "unreadable: ...", so the
    reconciliation never reaches dbt half-read and can never fail the build; release_changes reports it."""
    import time

    import duckdb

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            files = release_files(lake, build_id)
            dest.mkdir(parents=True, exist_ok=True)
            out = {}
            con = lake.connect() if lake.remote else duckdb.connect()
            try:
                for t in RECONCILED_TABLES:
                    if t not in files:
                        continue
                    local = dest / f"{t}.parquet"
                    src = files[t].replace("'", "''")
                    con.execute(
                        f"copy (select * from read_parquet('{src}', hive_partitioning = false)) "
                        f"to '{local}' (format parquet)"
                    )
                    cols = con.execute(
                        f"select column_name from (describe select * from read_parquet('{local}'))"
                    ).fetchall()
                    out[t] = {"path": str(local), "columns": [c for (c,) in cols]}
            finally:
                con.close()
            return out, "ok"
        except Exception as e:  # noqa: BLE001 - duckdb.IOException / HTTPException, a listing error
            last = e
            if attempt + 1 < attempts:
                time.sleep(2 ** (attempt + 1))
    log.warning("release %s not reconciled: its files couldn't be read (%s)", build_id, last)
    return {}, f"unreadable: {type(last).__name__}"


def release_change_counts(con) -> list[tuple[tuple[str, str, str], int]]:
    """release_changes counted per (table, change, reason). A build without the model (it errored, or the report
    step never ran) is logged and counted as one ('release', 'not_reconciled', 'release_changes_missing'), so its
    record says so instead of looking like a release with nothing served before it."""
    import duckdb

    if con is None:
        return []
    try:
        rows = con.execute(
            "select table_name, change, reason, count(*) from release_changes group by all order by all"
        ).fetchall()
    except duckdb.Error as e:
        log.warning(
            "release notes: release_changes is missing or failed, so this build isn't reconciled (%s)", e
        )
        return [(("release", "not_reconciled", "release_changes_missing"), 1)]
    return [((t, c, r), n) for t, c, r, n in rows]


def release_notes(db: Path) -> list[str]:
    """What this build (`db`) changes against the served release, one line per public table: the rows added,
    removed and changed, each split by reason, then what couldn't be compared (a column or table the served release
    lacks, or a release that couldn't be read). Unexplained and incomplete ones are what release_changes_explained
    warns on."""
    import duckdb

    if not db.exists():
        return []
    con = duckdb.connect(str(db), read_only=True)
    try:
        counts = release_change_counts(con)
    finally:
        con.close()
    out = []
    for table in RECONCILED_TABLES:
        parts = []
        for change in ("added", "removed", "changed"):
            reasons = [(r, n) for (t, c, r), n in counts if t == table and c == change]
            if reasons:
                total = sum(n for _, n in reasons)
                parts.append(f"{change} {total:,} (" + ", ".join(f"{r} {n:,}" for r, n in reasons) + ")")
        if parts:
            out.append(f"{table}: " + "; ".join(parts))
    incomplete = [f"{t} {c} {r}" for (t, c, r), _ in counts if c in ("schema", "not_reconciled")]
    if incomplete:
        out.append("not compared in full: " + "; ".join(incomplete))
    return out


def table_hash(con, relation: str) -> tuple[int, str]:
    """(rows, content hash) of a table, or of any relation such as a read_parquet() of a release file.

    Each row is encoded canonically: its columns but PER_BUILD_COLUMNS, sorted by name, as DuckDB's JSON of the row
    (to_json: lists keep their order, structs their fields; timestamps in UTC), and hashed with md5. The table's hash
    is a sha256 over HASH_METHOD, the column names, the row count and the sums of the rows' hashes (as two 64-bit
    halves, in 128-bit integers), so it doesn't depend on row order but does on every row, duplicates included."""
    cur = con.cursor()
    try:
        cur.execute("set session TimeZone = 'UTC'")  # timestamptz values render in UTC, wherever this runs
        names = [r[0] for r in cur.execute(f"describe select * from {relation}").fetchall()]
        cols = sorted(c for c in names if c not in PER_BUILD_COLUMNS)
        sel = ", ".join(f'"{c}"' for c in cols)
        row = cur.execute(
            "select count(*), coalesce(sum(('0x' || h[1:16])::ubigint::hugeint), 0), "
            "coalesce(sum(('0x' || h[17:32])::ubigint::hugeint), 0) "
            f"from (select md5(to_json(t)::varchar) as h from (select {sel} from {relation}) t)"
        ).fetchone()
    finally:
        cur.close()
    n, hi, lo = row if row else (0, 0, 0)
    digest = hashlib.sha256(f"{HASH_METHOD}|{','.join(cols)}|{n}|{hi}|{lo}".encode()).hexdigest()
    return int(n), digest


def build_hashes(lake: Lake, build_id: str) -> dict[str, tuple[int, str, str]]:
    """What a build recorded for each release table: {table: (rows, content hash, hash method)}. Empty for a build
    from before hashes were recorded."""
    rows = (
        lake.connect()
        .execute(
            f"select unique_id, rows, checksum, payload from {lake.scan('builds')} "
            "where build_id = ? and kind = 'table_hash'",
            [build_id],
        )
        .fetchall()
    )
    return {t: (int(n), h, m) for t, n, h, m in rows}


def build_inputs(lake: Lake, build_id: str) -> dict:
    """What a rebuild of this build needs to match: its run date, code (git_sha: the image's commit for a build in the
    ingest image; "unknown" for one whose image was built without it, and null for one recorded before the image
    carried it: use the image the build job ran), package versions and dbt vars that decide
    the output (verdict model and definition, max_failures), from its build record and run_results."""
    con = lake.connect()
    b = f"(select * from {lake.scan('builds')} where build_id = ?)"
    row = con.execute(
        f"select run_date::varchar, git_sha, dbt_version, package_versions, ci_run_url from {b} "
        "where kind = 'build'",
        [build_id],
    ).fetchone()
    if not row:
        return {}
    out = dict(
        zip(("run_date", "git_sha", "dbt_version", "package_versions", "ci_run_url"), row, strict=True)
    )
    out["package_versions"] = json.loads(out["package_versions"]) if out["package_versions"] else None
    results = con.execute(
        f"select payload from {b} where kind = 'artifact' and unique_id = 'run_results.build.json'",
        [build_id],
    ).fetchone()
    dbt_vars = ((json.loads(results[0]) if results else {}).get("args") or {}).get("vars") or {}
    keys = ("verdict_model", "verdict_definition", "max_failures")
    return out | {"dbt_vars": {k: dbt_vars.get(k) for k in keys} if dbt_vars else None}


def verify_release(lake: Lake, build_id: str) -> dict[str, tuple[int, str, str]]:
    """The hashes of a build's release files as they are now, to check against what the build recorded."""
    con = lake.connect()
    out = {}
    for t in sorted(release_files(lake, build_id)):
        n, h = table_hash(con, _release(lake, build_id, t))
        out[t] = (n, h, HASH_METHOD)
    return out


def compare_hashes(
    old: dict[str, tuple[int, str, str]], new: dict[str, tuple[int, str, str]]
) -> dict[str, dict[str, object]]:
    """Per table in either: whether it's the same (rows and hash both equal), and what differs. Hashes made by two
    methods can't be compared: that is reported as a method difference, never as different content."""
    out: dict[str, dict[str, object]] = {}
    for t in sorted(old.keys() | new.keys()):
        a, z = old.get(t), new.get(t)
        if a is None or z is None:
            out[t] = {"same": False, "missing_from": "old" if a is None else "new"}
            continue
        d: dict[str, object] = {"same": a == z, "rows": {"old": a[0], "new": z[0]}}
        if a[2] != z[2]:
            d["method"] = {"old": a[2], "new": z[2]}
        elif a[1] != z[1]:
            d["hash"] = {"old": a[1], "new": z[1]}
        out[t] = d
    return out


def about(n: float) -> str:
    """A count rounded to two significant figures for copy ("about 41,000"): what a stale coverage claim should say
    now, computed from the build rather than worked out by hand."""
    n = int(n + 0.5)  # halves round up, here and below: 1,850 is "about 1,900"
    if n < 100:
        return f"{n:,}"
    step = 10 ** (len(str(n)) - 2)
    return f"{(n + step // 2) // step * step:,}"


def coverage_claims(path: Path | None = None, strict: bool = True) -> list[dict]:
    """The numeric claims in `path`'s (default LISTING) coverage-claims comment, one `metric: text` line each: the
    metric in mart_build_profile, the text as it appears in the copy, its number (the stated value) and `source`,
    the file and line of the copy to update. Strict (CI's test), a claim whose text isn't in the copy or has no
    number raises ValueError, so the copy and its claims can't drift apart. Not strict (the build), that claim is
    logged and skipped, and a missing file is logged and gives no claims: the check never stops a build."""
    path = path or LISTING
    shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        if strict:
            raise
        log.warning("coverage claims not checked: %s can't be read (%s)", shown, e)
        return []
    block = _CLAIMS.search(text)
    if not block:
        return []
    copy = (text[: block.start()] + "\n" * block.group(0).count("\n") + text[block.end() :]).splitlines()
    claims = []
    for line in block.group(1).splitlines():
        if not (m := _CLAIM.match(line.strip())):
            continue  # a note, not a claim
        metric, said = m.groups()
        number = _NUMBER.search(said)
        lineno = next((i for i, c in enumerate(copy, 1) if said in c), None)
        if number is None or lineno is None:
            why = "has no number" if number is None else "is not in the copy"
            problem = f"{shown}: coverage claim {metric}: {said!r} {why}"
            if strict:
                raise ValueError(problem)
            log.warning("%s; not checked", problem)
            continue
        claims.append({"metric": metric, "value": float(number.group(0).replace(",", "")), "text": said,
                       "source": f"{shown}:{lineno}"})  # fmt: skip
    return claims


def failures_table(con, test: str) -> str | None:
    """The table where dbt stored a test's failing rows (store_failures, or `--store-failures`), quoted, or None if
    the test didn't store any this build."""
    schema = con.execute(
        "select any_value(table_schema) from information_schema.tables where table_name = ?", [test]
    ).fetchone()
    return None if not schema or schema[0] is None else f'"{schema[0]}"."{test}"'


def coverage_claim_messages(db: Path) -> list[str]:
    """What the coverage-claim tests found in this build (`db`), one line per claim, naming the line to update."""
    import duckdb

    if not db.exists():
        return []
    con = duckdb.connect(str(db), read_only=True)
    out = []
    try:
        for test, sql in (
            ("coverage_claims_name_metrics", "select source, metric, text from {t}"),
            ("coverage_claims_match_build", "select source, metric, text, built, off_by, tolerance from {t}"),
        ):
            table = failures_table(con, test)
            if table is None:
                continue  # the test didn't run (no claims, or the build stopped before the tests)
            for r in con.execute(sql.format(t=table)).fetchall():
                if test == "coverage_claims_name_metrics":
                    out.append(
                        f"{r[0]}: coverage claim {r[2]!r} names {r[1]}, which mart_build_profile doesn't have"
                    )
                else:
                    off = "far" if r[4] is None else f"{r[4]:+.0%}"  # None: the build has 0
                    now = f" (about {about(r[3])} now)" if r[3] >= 100 else ""  # a count, not a 0-1 share
                    out.append(
                        f"{r[0]}: {r[2]!r} is {off} off this build's {r[1]} ({r[3]:,.0f}; tolerance "
                        f"{r[5]:.0%}): update the line{now} and its coverage-claims entry, then the Snowflake "
                        "listing"
                    )
    finally:
        con.close()
    return out


def verdict_coverage_message(db: Path) -> str | None:
    """The share of listed coffees that Claude's verdicts decided in this build (dim_coffee.classified_by), by
    model: what verdicts_cover_catalog judges. None when dim_coffee wasn't built."""
    import duckdb

    if not db.exists():
        return None
    con = duckdb.connect(str(db), read_only=True)
    try:
        built = con.execute(
            "select count(*) from information_schema.tables where table_name = 'dim_coffee'"
        ).fetchone()
        if not built or not built[0]:
            return None
        counts = con.execute(
            "select count(*), count(*) filter (where classified_by = 'claude') from dim_coffee where is_listed"
        ).fetchone()
        listed, by_claude = counts or (0, 0)
        models = con.execute(
            "select verdict_model, count(*) from dim_coffee where is_listed and classified_by = 'claude'"
            " group by 1 order by 2 desc"
        ).fetchall()
    finally:
        con.close()
    share = f"{by_claude / listed:.1%}" if listed else "n/a"
    named = ", ".join(f"{m} {n:,}" for m, n in models) or "no verdict model"
    return f"{by_claude:,} of {listed:,} listed coffees ({share}) decided by Claude's verdicts ({named}); the rest by the rules"


def tag_parity_messages(db: Path, examples: int = 5) -> list[str]:
    """Products whose raw tags (what registry.py, enrich.py and verdict.py read) and decoded tags (what the build
    reads) give different decisions (int_tag_decoding_parity; the python_readers_agree_on_tags test fails the
    build on any): a count, then a few examples."""
    import duckdb

    if not db.exists():
        return []
    con = duckdb.connect(str(db), read_only=True)
    try:
        built = con.execute(
            "select count(*) from information_schema.tables where table_name = 'int_tag_decoding_parity'"
        ).fetchone()
        if not built or not built[0]:
            return []  # the build stopped before the model
        rows = con.execute(
            "select roaster_id, product_id, any_value(title), any_value(raw_tags), any_value(tags),"
            " list_sort(list_distinct(flatten(list(differs)))) from int_tag_decoding_parity"
            " group by roaster_id, product_id order by roaster_id, product_id"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return []
    out = [
        f"{len(rows)} products decide differently on raw and decoded tags (registry.py, enrich.py and verdict.py "
        "read raw tags): decode tags in products.item, or fix the rule"
    ]
    for rid, pid, title, raw, tags, differs in rows[:examples]:
        out.append(f"{rid}:{pid} {title!r}: {', '.join(differs)} differ; raw tags {raw}, decoded {tags}")
    return out


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
    """A release's tables: staged locally until published, then read from the remote lake. Only the files directly
    in the build's folder: its ops tables (ops/, OPS_TABLES) are never release tables."""
    staged = not (lake.read_only and lake.remote)  # a remote reader stages nothing: read the remote lake only
    local = (
        {p.stem: str(p) for p in lake.root.glob(f"releases/*/build={build_id}/*.parquet")} if staged else {}
    )
    if local or not lake.remote:
        return local
    return {
        Path(f).stem: f"{lake.url}/{f}"
        for f in _remote_releases(lake.url)
        if Path(f).parent.name == f"build={build_id}"
    }


def ops_files(lake: Lake, build_id: str) -> dict[str, str]:
    """A build's ops tables (OPS_TABLES), staged locally until published, then read from the remote lake."""
    staged = not (lake.read_only and lake.remote)
    local = (
        {p.stem: str(p) for p in lake.root.glob(f"releases/*/build={build_id}/ops/*.parquet")}
        if staged
        else {}
    )
    if local or not lake.remote:
        return local
    return {
        Path(f).stem: f"{lake.url}/{f}"
        for f in _remote_releases(lake.url)
        if Path(f).parent.name == "ops" and Path(f).parent.parent.name == f"build={build_id}"
    }


def forget_listings() -> None:
    """Drop the remote listings kept for the life of the process (the releases/ tree, the top-level folders), so a
    long-running reader (the admin) sees releases written since. A job reads once and never needs this."""
    from .lake import _REMOTE_FOLDERS

    _remote_releases.cache_clear()
    _REMOTE_FOLDERS.clear()


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
    """The newest run_date with any fetch attempt, or any run manifest, in the lake: a run that died after
    publishing its manifest but before its first fetch is still the run a build judges (and fails as incomplete)."""
    row = (
        lake.connect()
        .execute(
            f"select max(run_date)::varchar from (select run_date from {lake.scan('fetches')} "
            f"where roaster_id is not null union all select run_date from {lake.scan('run_manifests')} "
            "where roaster_id is not null)"
        )
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


def materialize(lake: Lake, build_id: str, db: Path = Path("build/served.duckdb")) -> Path:
    """Copy a release's tables into `db` (for the site or a public export) and change nothing else: no lake file,
    marts/ or serving_log is written. Returns `db`."""
    import duckdb

    _materialize(lake, build_id, db).close()
    duckdb.connect(str(db), read_only=True).close()  # flushed and readable
    return db


def _materialize(lake: Lake, build_id: str, db: Path):
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
    return con


def serve(
    lake: Lake,
    build_id: str,
    reason: str,
    db: Path = Path("build/served.duckdb"),
    action: str = "serve",
    previous: str | None | object = _UNREAD,
) -> Path:
    """Make a release the one customers get: its catalog marts replace marts/, marts/release.parquet records it,
    serving_log gets a row for it (`action`: SERVING_ACTIONS; `previous`: the release it replaces, read from
    marts/release.parquet when not given; None: nothing was served), and `db` gets every release table, for the public export and the site.
    Returns `db`."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    if action not in SERVING_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(SERVING_ACTIONS)}")
    passed = {b: (d, ok) for b, d, ok in builds(lake)}
    if build_id not in passed:
        raise ValueError(f"no build {build_id}")
    run_date, ok = passed[build_id]
    if not ok:
        raise ValueError(f"build {build_id} failed its checks; only a passing release can be served")
    if previous is _UNREAD:  # None is an answer (nothing served yet), not a reason to read again
        previous = (served(lake) or {}).get("build_id")
    con = _materialize(lake, build_id, db)
    for t in SERVED_MARTS:
        lake.write_mart(t, con, f"select * from {t}")
    pointer = lake.root / "marts" / "release.parquet"
    now = datetime.now(UTC).replace(microsecond=0)
    row = {"build_id": build_id, "run_date": run_date, "served_at": now, "reason": reason}
    pq.write_table(pa.Table.from_pylist([row]), pointer)
    ci_run_id, ci_run_url = lineage.ci_run()
    lake.write("serving_log", f"serving_log/serve_id={new_batch_id(now)}.parquet", [{
        "served_at": now, "action": action, "build_id": build_id, "run_date": date.fromisoformat(run_date),
        "reason": reason, "actor": lineage.actor(), "previous_build_id": previous,
        "ci_run_id": ci_run_id, "ci_run_url": ci_run_url}])  # fmt: skip
    con.close()
    duckdb.connect(str(db), read_only=True).close()  # flushed and readable
    return db


def rollback(lake: Lake, to: str | None = None, reason: str = "") -> str:
    """Serve an earlier passing release: `to` ('served': the one served now), or the passing release before the one
    served now. Serving the release served now again (to 'served', or its build id) is a re-export, logged as
    action reexport, not rollback: the served release doesn't change."""
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
    action = "reexport" if to == now else "rollback"
    serve(lake, to, reason or f"{action} from {now}", action=action, previous=now)
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
