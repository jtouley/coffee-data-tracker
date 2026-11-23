"""How the week's crawl went, for Week: each connector this week against last, each pass, Tuesday's chain, and what is
abnormal. Pure functions over the attempt rows queries.week returns (job_manifests, the latest row per attempt), so
the comparison and the flags are tested without a lake.

Nothing here is a new threshold:
- an attempt has died when it has only its running row past its job's time limit (fetchhealth.JOB_LIMIT, the
  crawl jobs' Cloud Run task timeout; CHECK_LIMIT for fetch-health's own polls), or once a later attempt of the same
  job and connector has started: fetch-health's rule;
- an IP's refusals are flagged above the egress check's share (control.REFUSAL_FLAG, 8% of first contacts); two
  weeks in a row is what the egress-check job itself fails on;
- a rise in errors or 429s is flagged when it is more than prod_health's warning margin of standard errors
  (K_SE["warning"], 4), reading last week's count as a Poisson count (its standard error is its square root).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from coffeedata.connectors import CONNECTORS
from coffeedata.control import REFUSAL_FLAG
from coffeedata.fetchhealth import CHECK_LIMIT, JOB_LIMIT
from coffeedata.prod_health import K_SE

CRAWL_JOBS = ("crawl", "crawl-retry")
# The crawl's three scheduled passes on the week's Monday (ingestion.tf's schedules), and where one ends: an attempt
# belongs to the pass whose slot it started in (a Cloud Run retry of the 09:17 crawl at 09:50 is still the morning).
PASSES = (
    ("morning", "Morning", time(9, 17)),
    ("resume", "Resume", time(15, 17)),
    ("retry", "Retry", time(21, 47)),
)
RESUME_FROM, RETRY_FROM = time(15, 0), time(21, 30)
CHAIN = (
    "merge",
    "enrich",
    "classify",
    "build",
)  # Tuesday: merge 03:17, enrich 04:17, build 06:17 (classify: by hand)
TUESDAY_DONE = time(
    12, 0
)  # the end of the Tuesday release window (jobs.in_window): a stage missing after it didn't run
JUMP_SE = K_SE["warning"]
WORST = ("failed", "died", "deadline", "running", "done")  # a pass's status: the worst of its attempts'


def _utc(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def monday(week: str) -> datetime:
    return datetime.combine(date.fromisoformat(week), time(0), UTC)


def is_pass(r: dict) -> bool:
    """A scheduled pass of the week's crawl: not a confirmation-run arm, a test crawl or a one-store (`--only`) run.
    The owner's test crawls are on Jobs; they never count, here or in the merge."""
    d = r.get("detail") or {}
    return (
        r["job"] in CRAWL_JOBS
        and not r.get("arm")
        and not (d.get("test") or d.get("only") or d.get("mode") == "only")
    )


def slot(r: dict, week: str) -> str:
    """The pass an attempt belongs to, by when it started (the Shopify retry job is always the retry)."""
    if r["job"] == "crawl-retry":
        return "retry"
    t = _utc(r["started_at"]) - monday(week)
    if t < timedelta(hours=RESUME_FROM.hour, minutes=RESUME_FROM.minute):
        return "morning"
    if t < timedelta(hours=RETRY_FROM.hour, minutes=RETRY_FROM.minute):
        return "resume"
    return "retry"


def died(rows: list[dict], now: datetime) -> set[str]:
    """Attempts with only their running row that have died (fetch-health's rule): past their job's time limit, or a
    later attempt of the same job and connector has started (a test crawl or an arm runs beside a pass, never after
    it)."""
    out = set()
    for r in rows:
        if r["status"] != "running":
            continue
        limit = CHECK_LIMIT if r["job"] == "fetch-health" else JOB_LIMIT
        start = _utc(r["started_at"])
        later = any(
            o["job"] == r["job"]
            and o["connector"] == r["connector"]
            and _utc(o["started_at"]) > start
            and (o["job"] not in CRAWL_JOBS or is_pass(o))
            for o in rows
        )
        if later or start + limit <= now:
            out.add(r["attempt_id"])
    return out


def state(r: dict, dead: set[str]) -> str:
    return "died" if r["attempt_id"] in dead else r["status"]


def _sum(rows: list[dict], key: str) -> int:
    return sum(r.get(key) or 0 for r in rows)


def totals(rows: list[dict], week: str, upto: str | None = None) -> dict[str, dict]:
    """Each connector's week from its passes (up to and including pass `upto`): stores that came back ok (each pass
    tries only the stores still failing, so the sum counts a store once), errors (the stores still failing after the
    latest crawl pass with an end row), 429 and 430 responses, and first-contact refusals (stores and refused)."""
    order = [p[0] for p in PASSES]
    keep = order[: order.index(upto) + 1] if upto else order
    out = {}
    for name in CONNECTORS:
        mine = [r for r in rows if r["connector"] == name and is_pass(r) and slot(r, week) in keep]
        ended = [r for r in mine if r["job"] == "crawl" and r["status"] != "running"]
        refusals = [x for r in mine for x in r.get("refusals") or []]
        out[name] = {
            "attempts": len(mine),
            "ok": _sum(mine, "ok"),
            "errors": (ended[-1]["errors"] or 0) if ended else 0,
            "http_429": _sum(mine, "http_429"),
            "http_430": _sum(mine, "http_430"),
            "stores": sum(x["stores"] or 0 for x in refusals),
            "refused": sum(x["refused"] or 0 for x in refusals),
        }
    return out


FIELDS = ("ok", "errors", "http_429", "http_430", "refused", "stores")


def compare(this: list[dict], last: list[dict], week: str, last_week: str) -> dict:
    """This week against last, per connector and in total. Last week is counted to the same pass this week has
    reached, so a Monday-morning look isn't compared with a whole week."""
    reached = [slot(r, week) for r in this if is_pass(r)]
    order = [p[0] for p in PASSES]
    upto = max(reached, key=order.index) if reached else None
    now_t, then_t = totals(this, week), totals(last, last_week, upto)
    rows: list[dict[str, Any]] = []
    for name in CONNECTORS:
        a, b = now_t[name], then_t[name]
        rows.append({"connector": name, "this": a, "last": b if b["attempts"] else None,
                     "delta": {k: a[k] - b[k] if b["attempts"] else None for k in FIELDS}})  # fmt: skip
    total_a = {k: sum(r["this"][k] for r in rows) for k in FIELDS}
    had = [r for r in rows if r["last"]]
    total_b = {k: sum(r["last"][k] for r in had) for k in FIELDS} if had else None
    return {
        "rows": [r for r in rows if r["this"]["attempts"] or r["last"]],
        "total": {
            "this": total_a,
            "last": total_b,
            "delta": {k: total_a[k] - total_b[k] if total_b else None for k in FIELDS},
        },  # fmt: skip
        "upto": upto,
    }


def jumped(this: int, last: int) -> bool:
    """More than chance would give: up by over JUMP_SE standard errors of last week's count (as a Poisson count)."""
    return this > last and this - last > JUMP_SE * math.sqrt(max(last, 1))


def passes(rows: list[dict], week: str, now: datetime, dead: set[str]) -> list[dict]:
    """Each scheduled pass: when it started, how it went (the worst of its attempts), and over its attempts that
    ended: the stores they tried, those that came back ok (after the morning: what it recovered) and those still
    failing. A pass with no attempt hasn't come yet, or didn't run."""
    out = []
    for key, label, at in PASSES:
        mine = [r for r in rows if is_pass(r) and slot(r, week) == key]
        ended = [
            r for r in mine if r["status"] != "running"
        ]  # counts only from end rows: tried, ok, failing agree
        statuses = {state(r, dead) for r in mine}
        if mine:
            st = next((s for s in WORST if s in statuses), sorted(statuses)[0])
        else:
            st = (
                "not yet"
                if now < monday(week) + timedelta(hours=at.hour, minutes=at.minute)
                else "didn't run"
            )
        out.append({
            "key": key, "label": label, "at": at.strftime("%H:%M"), "status": st,
            "started_at": min((r["started_at"] for r in mine), default=None),
            "connectors": sorted({r["connector"] for r in mine}),
            "ended": len(ended), "tried": _sum(ended, "todo") if mine else None,
            "ok": _sum(ended, "ok") if mine else None, "errors": _sum(ended, "errors") if mine else None,
        })  # fmt: skip
    return out


def refusal_shares(rows: list[dict]) -> dict[str, dict]:
    """Per egress IP: first contacts and refusals over the week's ended crawl passes (the egress check's sums)."""
    out: dict[str, dict] = {}
    for r in rows:
        if r["job"] != "crawl" or r["status"] == "running" or not is_pass(r):
            continue
        for x in r.get("refusals") or []:
            c = out.setdefault(x["ip"], {"stores": 0, "refused": 0, "connectors": set()})
            c["stores"] += x["stores"] or 0
            c["refused"] += x["refused"] or 0
            c["connectors"].add(r["connector"])
    for c in out.values():
        c["share"] = c["refused"] / c["stores"] if c["stores"] else 0.0
        c["connectors"] = sorted(c["connectors"])
    return out


def chain(week: dict, served: dict | None, now: datetime, dead: set[str]) -> list[dict]:
    """Tuesday: merge, enrich, (classify, only when it ran), the build's verdict, and whether this week's release is
    the one served. Each with its status and when."""
    out = []
    for job in CHAIN:
        mine = [r for r in week["rows"] if r["job"] == job]
        if job == "classify" and not mine:
            continue
        last = mine[-1] if mine else None
        st = state(last, dead) if last else "missing"
        if job == "build" and week["builds"]:
            b = week["builds"][0]
            out.append({"job": job, "status": "passed" if b["passed"] else "failed", "at": b["built_at"],
                        "build_id": b["build_id"]})  # fmt: skip
            continue
        out.append({"job": job, "status": st, "at": last["ended_at"] or last["started_at"] if last else None})
    served_week = str(served.get("run_date"))[:10] if served else None
    published = [b for b in week["builds"] if b.get("served_at")]
    if published:  # served once, even if a later week's release has replaced it since
        pub = {"job": "publish", "status": "published", "at": published[0]["served_at"],
               "build_id": published[0]["build_id"]}  # fmt: skip
    elif week["builds"]:
        pub = {"job": "publish", "status": "not published", "at": None, "serving": served_week}
    else:
        pub = {"job": "publish", "status": "missing", "at": None}
    out.append(pub)
    after = monday(week["week"]) + timedelta(days=1, hours=TUESDAY_DONE.hour)
    for c in out:  # before Tuesday's window ends, a stage that hasn't run yet isn't missing
        if c["status"] == "missing" and now < after:
            c["status"] = "not yet"
    return out


def flags(week: dict, last: dict | None, served: dict | None, now: datetime) -> dict:
    """Everything Week shows about the crawl's health, with the abnormal things first. Each flag is a code with its
    facts (part_week.html words them); `bad` ones are failures, `warn` ones need a look."""
    rows, wk = week["rows"], week["week"]
    last_rows = last["rows"] if last else []
    dead = died(rows, now)
    found: list[dict] = []
    for r in rows:
        if r["job"] in CRAWL_JOBS and not is_pass(r):
            continue  # a test crawl, an arm or a one-store run: on Jobs, never the week's
        where = {"job": r["job"], "connector": r["connector"], "started_at": r["started_at"],
                 "pass": slot(r, wk) if is_pass(r) else None}  # fmt: skip
        if r["attempt_id"] in dead:
            found.append({"code": "died", "kind": "bad", **where,
                          "hours": round((now - _utc(r["started_at"])).total_seconds() / 3600, 1)})  # fmt: skip
        elif r["status"] == "failed" and not (
            r["job"] == "build" and week["builds"]
        ):  # the verdict says it, below
            found.append({"code": "failed", "kind": "bad", **where})
    deadlines: dict[str, list[str]] = {}
    for r in rows:
        if is_pass(r) and r["status"] == "deadline":
            deadlines.setdefault(r["connector"], []).append(slot(r, wk))
    for name, slots in deadlines.items():
        found.append({"code": "deadline", "kind": "warn", "connector": name, "passes": slots})
    cmp = compare(rows, last_rows, wk, last["week"] if last else wk)
    for r in cmp["rows"]:
        if not r["last"]:
            continue
        ups = [{"field": key, "this": r["this"][key], "last": r["last"][key]}
               for key in ("errors", "http_429") if jumped(r["this"][key], r["last"][key])]  # fmt: skip
        if ups:  # one line per connector
            found.append({"code": "jump", "kind": "warn", "connector": r["connector"], "ups": ups})
    shares, before = refusal_shares(rows), refusal_shares(last_rows)
    for ip, c in sorted(shares.items()):
        if c["stores"] and c["share"] > REFUSAL_FLAG:
            prev = before.get(ip)
            found.append({"code": "refusals", "kind": "warn", "ip": ip, **c,
                          "twice": bool(prev and prev["stores"] and prev["share"] > REFUSAL_FLAG)})  # fmt: skip
    steps = chain(week, served, now, dead)
    for c in steps:
        if c["job"] == "build" and c["status"] == "failed":
            found.append({"code": "build_failed", "kind": "bad", "build_id": c.get("build_id")})
    gap = next((c for c in steps if c["status"] == "missing" and c["job"] != "publish"), None)
    if gap:  # the first stage that never ran: the ones after it wait on it
        found.append({"code": "chain_missing", "kind": "warn", "job": gap["job"]})
    found.sort(key=lambda f: f["kind"] != "bad")  # failures first, then the rest in the page's order
    return {
        "flags": found,
        "compare": cmp,
        "passes": passes(rows, wk, now, dead),
        "chain": steps,
        "died": dead,
        "refusal_flag": REFUSAL_FLAG,
        "last_week": last["week"] if last else None,
    }
