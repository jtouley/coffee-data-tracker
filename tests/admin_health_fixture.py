"""A synthetic lake for Week's crawl health, shaped like the production `job_manifests` (its columns, the running row
then the end row of each attempt, the 07:17 probe, the five crawls at 09:17 with their 15:17 resume and 21:47 retry,
fetch-health's polls, Tuesday's merge 03:17, enrich 04:17 and build 06:17, and counts of the same size), with made-up
stores (*.example) and documentation IPs (RFC 5737). No production row is copied.

Four weeks:
- W0: fetches only (the history the "failing week after week" streaks count back through);
- W1: a clean week: every pass done, Tuesday's chain done, its build passed and was served;
- W2: last week: woocommerce stops at its deadline, Tuesday done, its build passed and is served now;
- W3: this week, on Monday night (NOW): generic's resume crawl died (only its running row, and its retry has
  started since), woocommerce hit its deadline twice (as last week) and its retry never started, generic hit its
  deadline and its errors and 429s jumped, the shared IP was refused by over 8% of stores, a fetch-health poll
  failed, and square_online's retry is still running; Tuesday hasn't come yet.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from coffeedata import control
from coffeedata.lake import Lake

W0, W1, W2, W3 = "2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"
NOW = datetime(2026, 11, 23, 23, 30, tzinfo=UTC)
BUILD1, BUILD2 = "20261110T061830-a1b2c3", "20261117T061840-d4e5f6"
# One IP per connector's egress, as in production: woocommerce and generic share one dedicated NAT IP.
IPS = {"shopify": "203.0.113.10", "squarespace": "198.51.100.20", "square_online": "198.51.100.30",
       "woocommerce": "203.0.113.40", "generic": "203.0.113.40"}  # fmt: skip

# (connector, slot) -> (status, todo, ok, errors, http_429, refused); the slot's start is (hour, minute) on Monday.
SLOTS = {"morning": (9, 19), "resume": (15, 20), "retry": (21, 48)}
WEEKS = {
    W1: {
        ("shopify", "morning"): ("done", 1031, 1028, 3, 0, 0),
        ("squarespace", "morning"): ("done", 318, 316, 2, 0, 0),
        ("square_online", "morning"): ("done", 180, 179, 1, 0, 0),
        ("woocommerce", "morning"): ("done", 221, 214, 7, 1, 2),
        ("generic", "morning"): ("done", 640, 566, 81, 28, 25),
        ("shopify", "resume"): ("done", 3, 0, 3, 0, 0),
        ("squarespace", "resume"): ("done", 2, 0, 2, 0, 0),
        ("square_online", "resume"): ("done", 1, 0, 1, 0, 0),
        ("generic", "resume"): ("done", 78, 7, 71, 22, 0),
        ("woocommerce", "resume"): ("done", 7, 2, 5, 0, 0),
        ("shopify", "retry"): ("done", 0, 0, 0, 0, 0),  # the crawl-retry job
        ("squarespace", "retry"): ("done", 2, 0, 2, 0, 0),
        ("square_online", "retry"): ("done", 1, 0, 1, 0, 0),
        ("woocommerce", "retry"): ("done", 5, 1, 4, 0, 0),
        ("generic", "retry"): ("done", 71, 2, 69, 20, 0),
    },
    W2: {
        ("shopify", "morning"): ("done", 1044, 1041, 3, 0, 0),
        ("squarespace", "morning"): ("done", 323, 321, 2, 0, 0),
        ("square_online", "morning"): ("done", 183, 182, 1, 0, 0),
        ("woocommerce", "morning"): ("deadline", 228, 222, 7, 2, 3),
        ("generic", "morning"): ("done", 661, 583, 84, 32, 28),
        ("shopify", "resume"): ("done", 1, 0, 1, 0, 0),
        ("squarespace", "resume"): ("done", 1, 0, 1, 0, 0),
        ("square_online", "resume"): ("done", 1, 0, 1, 0, 0),
        ("woocommerce", "resume"): ("deadline", 9, 3, 6, 0, 0),
        ("generic", "resume"): ("done", 74, 6, 71, 26, 0),
        ("square_online", "retry"): ("done", 1, 0, 1, 0, 0),
        ("squarespace", "retry"): ("done", 1, 0, 1, 0, 0),
        ("woocommerce", "retry"): ("deadline", 9, 3, 6, 2, 0),
        ("generic", "retry"): ("done", 68, 1, 71, 25, 0),
        ("shopify", "retry"): ("done", 0, 0, 0, 0, 0),
    },
    W3: {
        ("shopify", "morning"): ("done", 1062, 1059, 3, 0, 0),
        ("squarespace", "morning"): ("done", 331, 322, 9, 0, 0),
        ("square_online", "morning"): ("done", 191, 184, 8, 0, 0),
        ("woocommerce", "morning"): ("deadline", 245, 236, 13, 0, 1),
        ("generic", "morning"): ("deadline", 765, 641, 131, 140, 91),
        ("shopify", "resume"): ("done", 2, 0, 2, 0, 0),
        ("squarespace", "resume"): ("done", 9, 0, 9, 0, 0),
        ("square_online", "resume"): ("done", 7, 0, 7, 0, 0),
        ("woocommerce", "resume"): ("deadline", 12, 4, 10, 0, 0),
        ("generic", "resume"): ("running", 124, None, None, None, None),  # died: the retry has started since
        ("generic", "retry"): ("done", 124, 4, 120, 12, 0),
        ("squarespace", "retry"): ("done", 9, 1, 8, 0, 0),
        ("square_online", "retry"): (
            "running",
            7,
            None,
            None,
            None,
            None,
        ),  # still going, well inside 4 hours
        ("shopify", "retry"): ("done", 2, 0, 2, 0, 0),
        # no woocommerce retry: it never started
    },
}


def _at(week: str, days: int, hour: int, minute: int = 17, second: int = 0) -> datetime:
    return datetime.combine(date.fromisoformat(week), datetime.min.time(), UTC) + timedelta(
        days=days, hours=hour, minutes=minute, seconds=second
    )


def _attempt(lake: Lake, week: str, job: str, connector: str | None, start: datetime, end: datetime | None,
             status: str, n: int, **counts) -> None:  # fmt: skip
    """The attempt's running row, then (unless it is still running) its end row."""
    aid = f"{start:%Y%m%dT%H%M%S}-{n:06x}"
    detail = {"execution": f"{job}-{(connector or 'all').replace('_', '-')}-{n:05x}", "task_attempt": 0}
    base = {"run_date": week, "job": job, "connector": connector, "attempt_id": aid, "started_at": start,
            "code_version": "test", "egress_ips": [IPS[connector]] if connector else []}  # fmt: skip
    control.write(lake, base | {"status": "running", "todo": counts.get("todo"), "detail": detail})
    if status != "running":
        control.write(lake, base | counts | {"status": status, "ended_at": end, "detail": detail})


def _crawls(lake: Lake, week: str) -> None:
    for n, ((conn, slot), (status, todo, ok, errors, h429, refused)) in enumerate(WEEKS[week].items(), 1):
        h, m = SLOTS[slot]
        start = _at(week, 0, h, m, n)
        end = start + timedelta(minutes=12 + todo // 30)
        counts = {"todo": todo}
        if status != "running":
            counts |= {"ok": ok, "errors": errors, "parked": 0, "skipped": 0, "requests": todo * 4,
                       "http_429": h429, "http_430": 0,
                       "refusals": [{"ip": IPS[conn], "stores": todo if slot == "morning" else 0, "refused": refused}]}  # fmt: skip
        job = (
            "crawl-retry" if conn == "shopify" and slot == "retry" else "crawl"
        )  # Shopify's 21:47 is its own job
        _attempt(lake, week, job, conn, start, end, status, n, **counts)
    _attempt(
        lake, week, "probe", None, _at(week, 0, 7, 17, 38), _at(week, 0, 7, 23), "done", 900, todo=732, ok=167
    )
    for i, (h, m) in enumerate(((10, 9), (15, 28), (15, 47))):
        st = "failed" if week == W3 and i == 2 else "done"
        _attempt(lake, week, "fetch-health", None, _at(week, 0, h, m), _at(week, 0, h, m + 4), st, 910 + i)
    if week in (W1, W2):  # Tuesday
        for job, h in (("merge", 3), ("enrich", 4), ("build", 6)):
            detail_build = {"build_id": BUILD1 if week == W1 else BUILD2}
            aid = f"{week}-{job}"
            start = _at(week, 1, h, 18, 8)
            base = {
                "run_date": week,
                "job": job,
                "attempt_id": aid,
                "started_at": start,
                "code_version": "test",
            }
            control.write(
                lake, base | {"status": "running", "detail": detail_build if job == "build" else {}}
            )
            control.write(lake, base | {"status": "done", "ended_at": start + timedelta(minutes=2 + 20 * (job == "build")),
                                        "detail": detail_build if job == "build" else {}})  # fmt: skip


def _fetch(week: str, rid: str, adapter: str, status: str, error: str | None = None, partial: bool = False,
           http: int | None = None) -> dict:  # fmt: skip
    return {"run_date": date.fromisoformat(week), "batch_id": "b", "fetched_at": _at(week, 0, 9, 30),
            "roaster_id": rid, "adapter": adapter, "status": status, "error": error, "products": 12,
            "http_statuses": [{"status": http or 200, "responses": 2}], "blocks": None, "partial": partial}  # fmt: skip


# Stores and the weeks each failed: two that keep failing, one that failed once, one partial every week.
STORES = {
    "always-down.example": ("generic", (W0, W1, W2, W3), "ConnectError: connection refused"),
    "blocked-since.example": ("woocommerce", (W2, W3), "HTTP 403 from the store API"),
    "one-bad-week.example": ("squarespace", (W3,), "HTTP 503"),
    "half-catalog.example": ("woocommerce", (W1, W2, W3), None),  # partial: ok, but stopped mid-catalog
}
# And 24 generic stores that time out two weeks running: enough for the list's second page.
STORES |= {
    f"slow-{i:02d}.example": ("generic", (W2, W3), "ReadTimeout: the store didn't answer in 30 s")
    for i in range(24)
}
OK_STORES = [f"roaster-{i:02d}.example" for i in range(12)]


def _fetches(lake: Lake, week: str) -> None:
    rows = [_fetch(week, rid, "shopify", "ok") for rid in OK_STORES]
    for rid, (adapter, bad, error) in STORES.items():
        if week not in bad:
            rows.append(_fetch(week, rid, adapter, "ok"))
        elif error is None:
            rows.append(_fetch(week, rid, adapter, "ok", partial=True))
        else:
            rows.append(_fetch(week, rid, adapter, "error", error, http=503 if "503" in error else 403))
    lake.write("fetches", f"fetches/run_date={week}/batch=a.parquet", rows)


def _builds(lake: Lake) -> None:
    for build, week in ((BUILD1, W1), (BUILD2, W2)):
        base = {"build_id": build, "built_at": _at(week, 1, 6, 40), "run_date": date.fromisoformat(week),
                "git_sha": "abc1234", "ci_run_url": None}  # fmt: skip
        lake.write("builds", f"builds/run_date={week}/build={build}.parquet",
                   [base | {"kind": "build", "unique_id": "build", "status": "pass"},
                    base | {"kind": "node", "unique_id": "test.run_ok_rate", "resource_type": "test", "status": "pass"}])  # fmt: skip
        lake.write("serving_log", f"serving_log/serve_id={build}.parquet",
                   [{"served_at": _at(week, 1, 6, 44), "action": "serve", "build_id": build,
                     "run_date": date.fromisoformat(week), "reason": "weekly", "actor": "the build job"}])  # fmt: skip
    (lake.root / "marts").mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist([{"build_id": BUILD2, "run_date": date.fromisoformat(W2),
                                          "served_at": _at(W2, 1, 6, 44), "reason": "weekly"}]),
                   lake.root / "marts" / "release.parquet")  # fmt: skip


def build_health_lake(root) -> Lake:
    lake = Lake(str(root))
    _fetches(lake, W0)
    for week in (W1, W2, W3):
        _crawls(lake, week)
        _fetches(lake, week)
    _builds(lake)
    return lake


def drop_connector(root, week: str, connector: str) -> None:
    """Remove one connector's every attempt from a week (a copy of the lake): the week it never ran."""
    for f in (root / "job_manifests" / f"run_date={week}").glob("*.parquet"):
        if pq.read_table(f).column("connector").to_pylist() == [connector]:
            f.unlink()
