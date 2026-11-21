"""Every admin page's data: plain queries over named lake files, through a read-only Lake. Nothing here writes,
summarises with a model or guesses: each function returns rows and counts, and the name of the file they came from.

Each function takes the lake and a DuckDB connection (`Lake.connect()`), so the caller can run it with a timeout and
interrupt it. Missing files are not errors: a table with no files yet reads as no rows.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from coffeedata import control, quality
from coffeedata.connectors import CONNECTORS
from coffeedata.lake import Lake, no_files

# A build passed when its gate row says so and no node failed: quality.builds' rule (and dbt stg_builds'), checked
# against it in tests/test_admin.py, written here once so the admin reads it through the request's connection.
PASSED = (
    "bool_or(kind = 'build' and status = 'pass') and not bool_or(coalesce(status, '') in ('error', 'fail'))"
)

STAGES = ("merge", "enrich", "classify", "build")
LANDED = ("done", "deadline")
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
HTTPS = re.compile(r"^https://[^\s\"'<>\\]+$")


def store_url(roaster_id: str | None) -> str | None:
    """A roaster's store, when its id is a hostname (they are: the store's domain)."""
    return f"https://{roaster_id}/" if roaster_id and HOST.match(roaster_id) else None


def safe_url(url: str | None) -> str | None:
    """Only https links are rendered; anything else (javascript:, data:, http) is shown as text, never linked."""
    return url if url and HTTPS.match(url) else None


def release_folder(lake: Lake, run_date: str, build_id: str) -> dict:
    """Where a build's release lives: a link to the dataset's folder on a remote lake, the path on a local one."""
    rel = f"releases/run_date={run_date}/build={build_id}"
    if lake.remote:
        repo, _, rev = lake.url.removeprefix("hf://datasets/").partition("@")
        return {"path": rel, "url": f"https://huggingface.co/datasets/{repo}/tree/{rev or 'main'}/{rel}"}
    return {"path": rel, "url": None}


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _empty_ok(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - duckdb.IOException; a table with no files yet
        if no_files(e) or "HTTP 404" in str(e):
            return default
        raise


def _iso(d: Any) -> str | None:
    return None if d is None else str(d)[:10]


# --- weeks and builds ---------------------------------------------------------------------------------------

WEEK = re.compile(r"run_date=(\d{4}-\d{2}-\d{2})")


def weeks(lake: Lake, con) -> list[str]:
    """Every week with fetches, job rows or builds, newest first, from the files' names alone (no file is read)."""
    found: set[str] = set()
    for table in ("fetches", "job_manifests", "builds"):
        for src in lake.sources(table):
            for (f,) in _empty_ok(lambda s=src: con.execute("select file from glob(?)", [s]).fetchall(), []):
                m = WEEK.search(f)
                if m and m.group(1) != "0000-00-00":
                    found.add(m.group(1))
    return sorted(found, reverse=True)


def builds(lake: Lake, con, run_date: str | None = None, drafts: bool = False) -> list[dict]:
    """Every build (or a week's), newest first: id, week, when, verdict, commit and CI run. A draft build never served
    is left out unless `drafts` (Release lists them): it isn't the week's build or a passing baseline, the rule of
    stg_builds and quality.unpublished_drafts."""
    where = "and run_date = ?" if run_date else ""
    if not drafts:
        where += f""" and build_id not in (
            select build_id from {lake.scan("builds")} where kind = 'draft' and build_id is not null
            except select build_id from {lake.scan("serving_log")} where build_id is not null)"""
    sql = f"""
        select build_id, any_value(run_date)::varchar as run_date, max(built_at) as built_at,
               {PASSED} as passed,
               any_value(git_sha) as git_sha, any_value(ci_run_url) as ci_run_url,
               any_value(dbt_version) as dbt_version, max(duration_s) as duration_s
        from {lake.scan("builds")} where build_id is not null {where}
        group by build_id order by max(built_at) desc, build_id desc"""
    rows = _empty_ok(lambda: _rows(con.execute(sql, [date.fromisoformat(run_date)] if run_date else [])), [])
    for r in rows:
        r["ci_run_url"] = safe_url(r["ci_run_url"])
    return rows


def served(lake: Lake, con) -> dict | None:
    """The release customers get now (marts/release.parquet), with its serving_log row: who served it, why, and
    the CI run that did."""
    src = ", ".join("'" + s.replace("'", "''") + "'" for s in lake.mart_sources("release"))
    try:  # quality.served's read, through this connection; only a missing file means nothing is served
        rows = _rows(con.execute(f"select * from read_parquet([{src}])"))
    except Exception as e:  # noqa: BLE001 - duckdb.IOException / HTTPException
        if no_files(e) or "HTTP 404" in str(e):
            return None
        raise
    cur = rows[0] if rows else None
    if not cur:
        return None
    out: dict[str, Any] = {k: (str(v) if v is not None else None) for k, v in cur.items()}
    log = _empty_ok(
        lambda: _rows(
            con.execute(
                f"select served_at, action, reason, actor, ci_run_url, previous_build_id "
                f"from {lake.scan('serving_log')} where build_id = ? order by served_at desc limit 1",
                [cur["build_id"]],
            )
        ),
        [],
    )
    out["log"] = log[0] if log else None
    if out["log"]:
        out["log"]["ci_run_url"] = safe_url(out["log"]["ci_run_url"])
    out["folder"] = release_folder(lake, _iso(cur["run_date"]) or "", cur["build_id"])
    return out


def week_build(lake: Lake, con, week: str) -> dict | None:
    """The week's latest build: the one whose ops tables describe the week."""
    rows = builds(lake, con, week)
    return rows[0] if rows else None


# --- Week ---------------------------------------------------------------------------------------------------


def job_rows(lake: Lake, con, run_date: str) -> list[dict]:
    """control.rows through this connection: the latest row per attempt (only a 'running' row: it died)."""
    q = f"""select * from {lake.scan("job_manifests")} where run_date = ?
            qualify row_number() over (partition by attempt_id order by (status <> 'running') desc,
                                       ended_at desc nulls last) = 1"""
    return [r for r in _rows(con.execute(q, [date.fromisoformat(run_date)])) if r["attempt_id"]]


def week(lake: Lake, con, run_date: str) -> dict:
    """Did the week land: every job attempt (job_manifests, the latest row per attempt), each connector's passes in
    order, the stages after the crawl, and the served release."""
    attempts = _empty_ok(lambda: job_rows(lake, con, run_date), [])
    # One order however DuckDB reads the files: jobs that start in the same second sort by job, connector, attempt.
    attempts.sort(key=lambda r: (r["started_at"], r["job"] or "", r["connector"] or "", r["attempt_id"]))
    by_connector: dict[str, int] = {}
    rows = []
    for r in attempts:
        d = control.detail_of(r)
        arm = control.is_arm(r)
        if r["job"] in ("crawl", "crawl-retry") and not arm:
            by_connector[r["connector"]] = by_connector.get(r["connector"], 0) + 1
            pass_no: int | None = by_connector[r["connector"]]
        else:
            pass_no = None
        rows.append(
            {
                "job": r["job"],
                "connector": r["connector"],
                "pass": pass_no,
                "arm": arm,
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "status": r["status"],
                "todo": r["todo"],
                "ok": r["ok"],
                "errors": r["errors"],
                "parked": r["parked"],
                "skipped": r["skipped"],
                "http_429": r["http_429"],
                "http_430": r["http_430"],
                "egress_ips": list(r["egress_ips"] or []),
                "execution": d.get("execution"),
                "build_id": d.get("build_id"),
                "code_version": r["code_version"],
            }
        )
    crawls = [r for r in rows if r["job"] in ("crawl", "crawl-retry") and not r["arm"]]
    connectors = []
    for name in CONNECTORS:
        mine = [r for r in crawls if r["connector"] == name]
        connectors.append(
            {
                "connector": name,
                "landed": any(r["status"] in LANDED for r in mine),
                "passes": len(mine),
                "latest": mine[-1]["status"] if mine else "missing",
            }
        )
    stages = {}
    for job in STAGES:
        mine = [r for r in rows if r["job"] == job]
        stages[job] = mine[-1]["status"] if mine else "missing"
    built = builds(lake, con, run_date)
    if built:  # the build's verdict, not just whether its job ended
        stages["build"] = "passed" if built[0]["passed"] else "failed"
    return {
        "week": run_date,
        "rows": rows,
        "connectors": connectors,
        "landed": sum(c["landed"] for c in connectors),
        "stages": stages,
        "builds": built,
        "source": ["job_manifests", "builds"],
    }


# --- Failures -----------------------------------------------------------------------------------------------


def _fields(con, scan: str, column: str) -> str:
    """The type of a nested column as DuckDB reads it (older files lack fields added later)."""
    rows = con.execute(f"select column_name, column_type from (describe select * from {scan})").fetchall()
    return next((typ for name, typ in rows if name == column), "")  # files from before the column: none


def _challenge_sql(con, lake: Lake) -> str:
    """dbt stg_fetches' challenge rule, on the fields this lake's fetches have."""
    t = _fields(con, lake.scan("fetches"), "blocks")
    arms = []
    if "sg_captcha" in t:
        arms.append(
            "when list_bool_or(list_transform(blocks, lambda b: b.sg_captcha is not null)) then 'siteground'"
        )
    if "cf_mitigated" in t:
        arms.append(
            "when list_bool_or(list_transform(blocks, lambda b: lower(b.cf_mitigated) = 'challenge')) then 'cloudflare'"
        )
    if not arms:
        return "null"
    return f"case when status = 'error' then case {' '.join(arms)} end end"


def failures(lake: Lake, con, run_date: str) -> dict:
    """Which stores failed this week and why: fetch counts by status and by HTTP status (fetches), challenge,
    partial and quarantined from the week's build's run_completeness when it has one (else from fetches, by the same
    rule), and every failed store with its evidence."""
    scan = lake.scan("fetches")
    d = date.fromisoformat(run_date)
    status = _empty_ok(
        lambda: dict(
            con.execute(
                f"select status, count(*) from {scan} where run_date = ? and roaster_id is not null group by 1",
                [d],
            ).fetchall()
        ),
        {},
    )
    http = _empty_ok(
        lambda: _rows(
            con.execute(
                f"""select h.status as http_status, count(distinct roaster_id) as stores, sum(h.responses) as responses
                    from {scan}, unnest(http_statuses) as t(h)
                    where run_date = ? and roaster_id is not null and h.status <> 200
                    group by 1 order by stores desc, http_status""",
                [d],
            )
        ),
        [],
    )
    build = week_build(lake, con, run_date)
    ops = quality.ops_files(lake, build["build_id"]) if build else {}
    challenge = _challenge_sql(con, lake) if status else "null"
    # Each roaster's week: its latest attempt, and every HTTP status it saw across attempts.
    stores = _empty_ok(
        lambda: _rows(
            con.execute(
                f"""with f as (select *, {challenge} as challenge from {scan}
                               where run_date = ? and roaster_id is not null),
                    h as (select roaster_id, list(distinct x.status order by x.status) as http_statuses
                          from f, unnest(f.http_statuses) as t(x) where x.status <> 200 group by roaster_id),
                    a as (select roaster_id, count(*) as attempts,
                                 arg_max(status, fetched_at) as status, arg_max(error, fetched_at) as error,
                                 arg_max(adapter, fetched_at) as adapter, max(fetched_at) as last_at,
                                 bool_or(status = 'ok') as any_ok, bool_or(coalesce(partial, false)) as partial,
                                 arg_max(challenge, fetched_at) filter (where challenge is not null) as challenge
                          from f group by roaster_id)
                    select a.*, coalesce(h.http_statuses, []) as http_statuses
                    from a left join h using (roaster_id)""",
                [d],
            )
        ),
        [],
    )
    completeness = None
    if "run_completeness" in ops:
        src = ops["run_completeness"].replace("'", "''")
        completeness = _rows(
            con.execute(
                f"""select roaster_id, attempted, ok, unavailable, quarantined, partial, partial_reason, challenge,
                           adapter, expected_from
                    from read_parquet('{src}', hive_partitioning = false)"""
            )
        )
    if completeness is not None:
        source = "run_completeness"
        counts = {
            "expected": len(completeness),
            "not_attempted": sum(not r["attempted"] for r in completeness),
            "failed": sum(bool(r["attempted"] and not r["ok"]) for r in completeness),
            "siteground": sum(r["challenge"] == "siteground" for r in completeness),
            "cloudflare": sum(r["challenge"] == "cloudflare" for r in completeness),
            "partial": sum(bool(r["partial"]) for r in completeness),
            "quarantined": sum(bool(r["quarantined"]) for r in completeness),
            "unavailable": sum(bool(r["unavailable"]) for r in completeness),
        }
        flags = {r["roaster_id"]: r for r in completeness}
    else:
        source = "fetches"
        counts = {
            "expected": None,
            "not_attempted": None,
            "failed": sum(not r["any_ok"] and r["status"] == "error" for r in stores),
            "siteground": sum(not r["any_ok"] and r["challenge"] == "siteground" for r in stores),
            "cloudflare": sum(not r["any_ok"] and r["challenge"] == "cloudflare" for r in stores),
            "partial": sum(bool(r["any_ok"] and r["partial"]) for r in stores),
            "quarantined": sum(r["status"] == "crashed" for r in stores),
            "unavailable": sum(r["status"] == "unavailable" for r in stores),
        }
        flags = {}
    failed = []
    for r in stores:
        f = flags.get(r["roaster_id"]) or {}
        bad = (
            (not r["any_ok"])
            or r["partial"]
            or f.get("partial")
            or f.get("quarantined")
            or (f and not f.get("ok"))
        )
        if not bad:
            continue
        failed.append(
            r
            | {
                "challenge": f.get("challenge", r["challenge"]),
                "quarantined": bool(f.get("quarantined")) or r["status"] == "crashed",
                "partial": bool(f.get("partial") or r["partial"]),
                "partial_reason": f.get("partial_reason"),
                "url": store_url(r["roaster_id"]),
                "error": (r["error"] or "")[:300] or None,
            }
        )
    failed.sort(key=lambda r: (r["any_ok"], r["challenge"] or "~", r["roaster_id"]))
    return {
        "week": run_date,
        "fetches": sum(status.values()),
        "by_status": status,
        "http": http,
        "counts": counts,
        "counts_source": source,
        "build_id": build["build_id"] if build else None,
        "stores": failed,
        "source": ["fetches", *(["run_completeness"] if source == "run_completeness" else [])],
    }


def failure_trend(lake: Lake, con, limit: int = 12) -> list[dict]:
    """Per week, newest first: fetch attempts, stores that never fetched ok, and stores that saw a 202 or a 429."""
    scan = lake.scan("fetches")
    return _empty_ok(
        lambda: _rows(
            con.execute(
                f"""with s as (
                        select run_date, roaster_id, bool_or(status = 'ok') as ok, count(*) as attempts,
                               bool_or(list_bool_or(list_transform(http_statuses, lambda h: h.status = 202))) as s202,
                               bool_or(list_bool_or(list_transform(http_statuses, lambda h: h.status = 429))) as s429
                        from {scan} where roaster_id is not null group by all)
                    select run_date::varchar as week, sum(attempts) as fetches, count(*) as stores,
                           count(*) filter (where not ok) as failed,
                           count(*) filter (where coalesce(s202, false)) as stores_202,
                           count(*) filter (where coalesce(s429, false)) as stores_429
                    from s group by run_date order by run_date desc limit {int(limit)}"""
            )
        ),
        [],
    )


# --- Gate ---------------------------------------------------------------------------------------------------


def previous_passing(all_builds: list[dict], build_id: str) -> str | None:
    """The last passing build before this one (all_builds: builds(), newest first)."""
    ids = [b["build_id"] for b in all_builds]
    if build_id not in ids:
        return None
    return next((b["build_id"] for b in all_builds[ids.index(build_id) + 1 :] if b["passed"]), None)


def gate(lake: Lake, con, build_id: str) -> dict:
    """The build's DQ tests (builds kind=node, resource_type test) with status and failures, failing first, and its
    metrics (kind=metric) against the previous passing build."""
    scan = lake.scan("builds")
    tests = _rows(
        con.execute(
            f"""select unique_id, status, failures, execution_s from {scan}
                where build_id = ? and kind = 'node' and resource_type in ('test', 'unit_test')
                order by case status when 'error' then 0 when 'fail' then 1 when 'warn' then 2 else 3 end,
                         unique_id""",
            [build_id],
        )
    )
    other = _rows(
        con.execute(
            f"""select resource_type, status, count(*) as n from {scan}
                where build_id = ? and kind = 'node' group by all order by all""",
            [build_id],
        )
    )
    all_builds = builds(lake, con)  # never an unpublished draft: one isn't anyone's baseline
    if build_id not in {
        b["build_id"] for b in all_builds
    }:  # this build is a draft: place it among them by time
        own = [b for b in builds(lake, con, drafts=True) if b["build_id"] == build_id]
        all_builds = sorted(all_builds + own, key=lambda b: (b["built_at"], b["build_id"]), reverse=True)
    prev = previous_passing(all_builds, build_id)
    metrics = _rows(
        con.execute(
            f"""with m as (select build_id, unique_id, value from {scan} where kind = 'metric' and build_id in (?, ?))
                select coalesce(a.unique_id, b.unique_id) as metric, a.value as value, b.value as previous
                from (select * from m where build_id = ?) a
                full join (select * from m where build_id = ?) b using (unique_id)
                order by metric""",
            [build_id, prev or "", build_id, prev or ""],
        )
    )
    for m in metrics:
        a, b = m["value"], m["previous"]
        m["change"] = None if a is None or b is None else a - b
        m["ratio"] = None if a is None or not b else a / b
    info = [b for b in all_builds if b["build_id"] == build_id]
    by_status: dict[str, int] = {}
    for t in tests:
        by_status[t["status"] or "none"] = by_status.get(t["status"] or "none", 0) + 1
    return {
        "build": info[0] if info else None,
        "previous": prev,
        "tests": tests,
        "by_status": by_status,
        "nodes": other,
        "metrics": metrics,
        "source": ["builds"],
    }


# --- Changes ------------------------------------------------------------------------------------------------


def changes(
    lake: Lake,
    con,
    build_id: str,
    table: str | None = None,
    change: str | None = None,
    reason: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """The build's release_changes (ops): counts by table, change and reason, and the rows, filtered."""
    ops = quality.ops_files(lake, build_id)
    if "release_changes" not in ops:
        return {"recorded": False, "build_id": build_id, "source": ["release_changes"]}
    src = f"read_parquet('{ops['release_changes'].replace(chr(39), chr(39) * 2)}', hive_partitioning = false)"
    counts = _rows(
        con.execute(
            f"select table_name, change, reason, count(*) as n from {src} group by all order by table_name, change, n desc, reason"
        )
    )
    where, params = [], []
    for col, val in (("table_name", table), ("change", change), ("reason", reason)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    w = f"where {' and '.join(where)}" if where else ""
    total = con.execute(f"select count(*) from {src} {w}", params).fetchone()[0]
    rows = _rows(
        con.execute(
            f"select table_name, item, change, reason from {src} {w} "
            f"order by reason = 'unexplained' desc, table_name, change, reason, item limit ? offset ?",
            [*params, int(limit), int(offset)],
        )
    )
    return {
        "recorded": True,
        "build_id": build_id,
        "counts": counts,
        "unexplained": sum(c["n"] for c in counts if c["reason"] == "unexplained"),
        "total": sum(c["n"] for c in counts),
        "matching": total,
        "rows": rows,
        "source": ["release_changes"],
    }


# --- Release and the guards: what the writes check against the lake ---------------------------------------------


def week_state(lake: Lake, con, week: str) -> dict:
    """For the job guards: has the week's merge a done row (job_manifests)."""
    rows = _empty_ok(lambda: job_rows(lake, con, week), [])
    return {
        "week": week,
        "merge_done": any(r["job"] == "merge" and r["status"] == "done" for r in rows),
    }


def releases(lake: Lake, con) -> dict:
    """Every build with its verdict, which are drafts and which were ever served: quality's one rule for each
    (quality.drafts, quality.ever_served). "Published" is derived from serving_log, never a status anyone sets."""
    all_builds = builds(lake, con, drafts=True)
    drafts = _empty_ok(lambda: quality.drafts(lake), {})
    ever = _empty_ok(lambda: quality.ever_served(lake), set())
    for b in all_builds:
        b["draft_id"] = drafts.get(b["build_id"])
        b["ever_served"] = b["build_id"] in ever
    return {"builds": all_builds, "by_draft": {d: b for b, d in drafts.items()}, "ever_served": ever}


def build_snapshot(lake: Lake, con, build_id: str) -> dict | None:
    """The decisions snapshot a build applied (quality.build_snapshot)."""
    return _empty_ok(lambda: quality.build_snapshot(lake, build_id), None)


def test_results(lake: Lake, con, test_ids: list[str]) -> dict[str, list[dict]]:
    """What each test crawl found (its job_manifests end row, detail.test = the id): the store answered when ok > 0."""
    if not test_ids:
        return {}
    rows = _empty_ok(lambda: _rows(con.execute(
        f"""select json_extract_string(detail, '$.test') as test_id, job, connector, status, ok, errors
            from {lake.scan("job_manifests")} where status <> 'running'
              and json_extract_string(detail, '$.test') in (select unnest(?::varchar[]))
            qualify row_number() over (partition by attempt_id order by ended_at desc nulls last) = 1""",
        [test_ids])), [])  # fmt: skip
    out: dict[str, list[dict]] = {}
    for r in rows:  # a full test crawl's five connectors share one test id
        out.setdefault(r["test_id"], []).append(r)
    return out


def leaving(lake: Lake, con, build_id: str, coffees: list[str] | None = None) -> dict:
    """How many public rows a build removes against the release it was compared with (release_changes): Publish
    asks the owner to type the count when it is over 50. `changed`: how many of `coffees` (a draft's decisions'
    coffees) change in public_coffee, whatever the reason (an undone include reads as reclassified)."""
    ops = quality.ops_files(lake, build_id)
    if "release_changes" not in ops:
        return {"recorded": False, "removed": None, "changed": None}
    src = f"read_parquet('{ops['release_changes'].replace(chr(39), chr(39) * 2)}', hive_partitioning = false)"
    removed = con.execute(f"select count(*) from {src} where change = 'removed'").fetchone()[0]
    changed = con.execute(
        f"select count(distinct item) from {src} where table_name = 'public_coffee' "
        "and item in (select unnest(?::varchar[]))",
        [coffees or []],
    ).fetchone()[0]
    return {"recorded": True, "removed": removed, "changed": changed}


def to_json(value: Any) -> str:
    return json.dumps(value, default=str)
