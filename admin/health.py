"""How the week's crawl went, for Week: each connector this week against last, each pass, Tuesday's chain, and what is
abnormal. Pure functions over the attempt rows queries.week returns (job_manifests, the latest row per attempt), so
the comparison and the flags are tested without a lake.

Nothing here is a new threshold, except the grace before a pass that hasn't started counts as missed:
- an attempt has died when it has only its running row past its job's time limit (fetchhealth.JOB_LIMIT, the
  crawl jobs' Cloud Run task timeout; CHECK_LIMIT for fetch-health's own polls), or once a later attempt of the same
  job and connector has started: fetch-health's rule;
- an IP's refusals are flagged above the egress check's share (control.REFUSAL_FLAG, 8% of first contacts);
- a rise in errors or 429s is flagged when it is more than prod_health's warning margin of standard errors
  (K_SE["warning"], 4), reading last week's count as a Poisson count;
- a scheduled pass (ingestion.tf's crons, checked by a test) is missed when a connector has no attempt in it
  MISSED_AFTER after its slot: Cloud Scheduler starts a job within seconds, and its first act is the running row.

A pass that hit its time limit is one whose end row says `deadline`: since the crawl's end status became truthful
(connectors.end_status), that means stores left unstarted or catalogs the deadline stopped mid-way. Catalogs read only
in part for any reason (detail.partial) are a calm line of their own, never called a time limit.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from coffeedata.connectors import CONNECTORS
from coffeedata.control import REFUSAL_FLAG
from coffeedata.fetchhealth import CHECK_LIMIT, JOB_LIMIT
from coffeedata.ingest import PARTIAL_WEEKS
from coffeedata.prod_health import K_SE

CRAWL_JOBS = ("crawl", "crawl-retry")
# The crawl's three scheduled passes on the week's Monday (ingestion.tf's schedules: every connector's job at each,
# Shopify's 21:47 as the crawl-retry job), and where one ends: an attempt belongs to the pass whose slot it started
# in (a Cloud Run retry of the 09:17 crawl at 09:50 is still the morning).
PASSES = (
    ("morning", "Morning", time(9, 17)),
    ("resume", "Resume", time(15, 17)),
    ("retry", "Retry", time(21, 47)),
)
ORDER = tuple(p[0] for p in PASSES)
RESUME_FROM, RETRY_FROM = time(15, 0), time(21, 30)
MISSED_AFTER = timedelta(minutes=30)
CHAIN = (
    "merge",
    "enrich",
    "classify",
    "build",
)  # Tuesday: merge 03:17, enrich 04:17, build 06:17 (classify: by hand)
TUESDAY_DONE = time(
    12, 0
)  # the Tuesday release window's end (jobs.in_window): a stage missing after it didn't run
JUMP_SE = K_SE["warning"]
WORST = ("failed", "died", "deadline", "running", "done")  # a pass's status: the worst of its attempts'


def _utc(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def monday(week: str) -> datetime:
    return datetime.combine(date.fromisoformat(week), time(0), UTC)


def slot_start(week: str, key: str) -> datetime:
    at = dict((k, t) for k, _, t in PASSES)[key]
    return monday(week) + timedelta(hours=at.hour, minutes=at.minute)


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
        later = (
            r["job"] not in CRAWL_JOBS or is_pass(r)
        ) and any(  # a test crawl or an arm is no pass's attempt
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


def _errors(rows: list[dict]) -> int:
    """Stores still failing: the latest ended crawl pass's errors, plus the latest ended Shopify retry's (it retries
    the stores parked for blocking us, which the crawl's errors don't count)."""
    out = 0
    for job in CRAWL_JOBS:
        ended = [r for r in rows if r["job"] == job and r["status"] != "running"]
        out += (ended[-1]["errors"] or 0) if ended else 0
    return out


def totals(rows: list[dict], connector: str, week: str, upto: str) -> dict:
    """One connector's week from its passes up to and including pass `upto`: stores that came back ok (each pass
    tries only the stores still failing, so the sum counts a store once), errors (still failing, _errors), 429 and 430
    responses, and first-contact refusals (stores and refused)."""
    keep = ORDER[: ORDER.index(upto) + 1]
    mine = [r for r in rows if r["connector"] == connector and is_pass(r) and slot(r, week) in keep]
    refusals = [x for r in mine for x in r.get("refusals") or []]
    return {
        "attempts": len(mine),
        "ok": _sum(mine, "ok"),
        "errors": _errors(mine),
        "http_429": _sum(mine, "http_429"),
        "http_430": _sum(mine, "http_430"),
        "stores": sum(x["stores"] or 0 for x in refusals),
        "refused": sum(x["refused"] or 0 for x in refusals),
    }


FIELDS = ("ok", "errors", "http_429", "http_430", "refused", "stores")


def compare(
    this: list[dict], last: list[dict], week: str, last_week: str, dead: set[str] | None = None
) -> dict:
    """This week against last, per connector and in total. Each connector is counted to its latest pass that has
    ended (a died attempt has ended), and last week to the same pass, so a Monday-morning look is never compared with
    a whole week, nor a pass in flight with a finished one. A connector with an attempt still running is `running`
    (its finished passes still count, but it gets no change and no rise check), as is one whose latest pass only
    died (`died`); one with no attempt this week `didn't run` (never zeros that read as an improvement)."""
    dead = dead or set()
    rows: list[dict[str, Any]] = []
    for name in CONNECTORS:
        mine = [r for r in this if r["connector"] == name and is_pass(r)]
        had_last = any(r["connector"] == name and is_pass(r) for r in last)
        if not mine:
            if had_last:
                rows.append({"connector": name, "status": "didn't run", "this": None, "last": None, "upto": None,
                             "delta": dict.fromkeys(FIELDS)})  # fmt: skip
            continue
        live = [slot(r, week) for r in mine if r["status"] == "running" and r["attempt_id"] not in dead]
        first_live = min(map(ORDER.index, live)) if live else len(ORDER)
        # counted to the latest pass with an end row before any running one: a pass whose attempts all died has
        # no counts, so it never reads as zeros (it is `died`, like `running`, while no later pass has ended)
        ended = [
            slot(r, week)
            for r in mine
            if r["status"] != "running" and ORDER.index(slot(r, week)) < first_live
        ]
        upto = max(ended, key=ORDER.index) if ended else None
        after = [
            slot(r, week) for r in mine if upto is None or ORDER.index(slot(r, week)) > ORDER.index(upto)
        ]
        status = "running" if live else "died" if after else None
        a = totals(this, name, week, upto) if upto else None
        b = totals(last, name, last_week, upto) if upto and had_last else None
        if b is not None and not b["attempts"]:
            b = None
        delta = {k: a[k] - b[k] for k in FIELDS} if a and b and not status else dict.fromkeys(FIELDS)
        rows.append({"connector": name, "status": status, "this": a, "last": b, "upto": upto,
                     "delta": delta})  # fmt: skip
    counted = [r for r in rows if r["this"]]
    both = [r for r in counted if r["delta"]["ok"] is not None]  # the All row's change: like for like only
    return {
        "rows": rows,
        "total": {
            "this": {k: sum(r["this"][k] for r in counted) for k in FIELDS} if counted else None,
            "delta": {k: sum(r["delta"][k] for r in both) if both else None for k in FIELDS},
            "partial": bool(both) and len(both) < len(rows),  # some connectors left out of the change
        },
        "upto": max((r["upto"] for r in counted), key=ORDER.index) if counted else None,
    }


def jumped(this: int, last: int) -> bool:
    """More than chance would give: up by over JUMP_SE standard errors of last week's count (as a Poisson count)."""
    return this > last and this - last > JUMP_SE * math.sqrt(max(last, 1))


def passes(rows: list[dict], week: str, now: datetime, dead: set[str]) -> list[dict]:
    """Each scheduled pass: when it started, how it went (the worst of its attempts), and over its attempts that
    ended: the stores they tried, those that came back ok (after the morning: what it recovered) and those still
    failing, with how many attempts are still running or died. A pass with no attempt hasn't come yet, or didn't
    run."""
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
            st = "not yet" if now < slot_start(week, key) + MISSED_AFTER else "didn't run"
        out.append({
            "key": key, "label": label, "at": at.strftime("%H:%M"), "status": st,
            "started_at": min((r["started_at"] for r in mine), default=None),
            "connectors": sorted({r["connector"] for r in mine}),
            "running": sum(r["status"] == "running" and r["attempt_id"] not in dead for r in mine),
            "died": sum(r["attempt_id"] in dead for r in mine),
            "tried": _sum(ended, "todo") if mine else None,
            "ok": _sum(ended, "ok") if mine else None, "errors": _sum(ended, "errors") if mine else None,
        })  # fmt: skip
    return out


def missed(rows: list[dict], week: str, now: datetime) -> list[dict]:
    """Each scheduled pass past its slot and MISSED_AFTER, with the connectors that have no attempt in it: all of
    them (the pass never started) or some (the others ran)."""
    out = []
    for key, _, _ in PASSES:
        if now < slot_start(week, key) + MISSED_AFTER:
            continue
        ran = {r["connector"] for r in rows if is_pass(r) and slot(r, week) == key}
        gone = [c for c in CONNECTORS if c not in ran]
        if gone:
            out.append({"pass": key, "connectors": gone, "whole": not ran})
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
    """Tuesday: merge, enrich, (classify, only when it ran), the build's verdict, and whether this week's release
    was served. Each with its status and when."""
    out = []
    for job in CHAIN:
        mine = [r for r in week["rows"] if r["job"] == job]
        if job == "classify" and not mine:
            continue
        last = mine[-1] if mine else None
        if job == "build" and week["builds"]:
            b = week["builds"][0]
            out.append({"job": job, "status": "passed" if b["passed"] else "failed", "at": b["built_at"],
                        "build_id": b["build_id"]})  # fmt: skip
            continue
        out.append({"job": job, "status": state(last, dead) if last else "missing",
                    "at": (last["ended_at"] or last["started_at"]) if last else None})  # fmt: skip
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


def _deadlines(rows: list[dict], week: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in rows:
        if is_pass(r) and r["status"] == "deadline":
            out.setdefault(r["connector"], []).append(slot(r, week))
    return out


def _partials(rows: list[dict], week: str) -> dict[str, dict]:
    """Per connector, its latest ended pass's catalogs read only in part (detail.partial: ok fetches marked partial,
    whatever stopped them) and, of those, the stores partial in each of their last PARTIAL_WEEKS runs
    (detail.partial_every_week). The latest pass, not a sum: each pass asks a partial store again (it isn't done),
    so a sum would count one store once per pass. Connectors with none are left out."""
    out: dict[str, dict] = {}
    for r in rows:  # in start order (queries.week): the latest ended pass wins
        if is_pass(r) and r["status"] not in ("running", "failed"):
            d = r.get("detail") or {}
            out[r["connector"]] = {"pass": slot(r, week), "partial": d.get("partial") or 0,
                                   "every_week": list(d.get("partial_every_week") or [])}  # fmt: skip
    return {c: v for c, v in out.items() if v["partial"]}


def flags(week: dict, last: dict | None, served: dict | None, now: datetime) -> dict:
    """Everything Week shows about the crawl's health, with the abnormal things first. Each flag is a code with its
    facts (part_week.html words them); `bad` ones are failures, `warn` ones need a look, and `usual` ones (a deadline
    in no more passes than last week's, catalogs read only in part) are listed after them without counting as new."""
    rows, wk = week["rows"], week["week"]
    last_rows = last["rows"] if last else []
    last_wk = last["week"] if last else wk
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
    due = missed(rows, wk, now)
    # a connector missing from every pass due so far (more than one): one line for its week, not one per pass
    absent = [c for c in CONNECTORS if len(due) > 1 and all(c in m["connectors"] for m in due)
              and not any(r["connector"] == c and is_pass(r) for r in rows)]  # fmt: skip
    if absent:
        found.append(
            {"code": "absent", "kind": "bad", "connectors": absent, "passes": [m["pass"] for m in due]}
        )
    for m in due:
        rest = [c for c in m["connectors"] if c not in absent]
        if rest:
            found.append({"code": "missed", "kind": "bad", **m, "connectors": rest})
    before = _deadlines(last_rows, last_wk)
    gone = {(c, m["pass"]) for m in missed(rows, wk, now) for c in m["connectors"]}
    for name, slots in _deadlines(rows, wk).items():
        # its stores wait for a later pass only if one ran, or is still to come (not missed)
        later = ORDER[max(map(ORDER.index, slots)) + 1 :]
        if (
            name == "shopify"
        ):  # its 21:47 pass is crawl-retry, which takes only parked stores (connectors.crawl_retry)
            later = tuple(k for k in later if k != "retry")
        final = all((name, key) in gone for key in later)
        prev = before.get(name, [])
        usual = bool(prev) and set(slots) <= set(prev)  # no worse than last week: the same line, calmly
        found.append({"code": "deadline", "kind": "usual" if usual else "warn", "connector": name, "passes": slots,
                      "last_passes": prev, "final": final})  # fmt: skip
    # partial catalogs: calm, since the gate passes and dbt keeps the unread products listed
    for name, part in _partials(rows, wk).items():
        found.append({"code": "partial", "kind": "usual", "connector": name, **part, "weeks": PARTIAL_WEEKS})
    cmp = compare(rows, last_rows, wk, last_wk, dead)
    for r in cmp["rows"]:
        if r["delta"]["ok"] is None:
            continue  # no like-for-like week to rise from, or a pass still running
        ups = [{"field": key, "this": r["this"][key], "last": r["last"][key]}
               for key in ("errors", "http_429") if jumped(r["this"][key], r["last"][key])]  # fmt: skip
        if ups:  # one line per connector
            found.append({"code": "jump", "kind": "warn", "connector": r["connector"], "ups": ups})
    shares, prev_shares = refusal_shares(rows), refusal_shares(last_rows)
    for ip, c in sorted(shares.items()):
        if c["stores"] and c["share"] > REFUSAL_FLAG:
            p = prev_shares.get(ip)
            found.append({"code": "refusals", "kind": "warn", "ip": ip, **c,
                          "twice": bool(p and p["stores"] and p["share"] > REFUSAL_FLAG)})  # fmt: skip
    steps = chain(week, served, now, dead)
    for c in steps:
        if c["job"] == "build" and c["status"] == "failed":
            found.append({"code": "build_failed", "kind": "bad", "build_id": c.get("build_id")})
    gap = next((c for c in steps if c["status"] == "missing" and c["job"] != "publish"), None)
    if gap:  # the first stage that never ran: the ones after it wait on it
        found.append({"code": "chain_missing", "kind": "warn", "job": gap["job"]})
    rank = {"bad": 0, "warn": 1, "usual": 2}
    found.sort(key=lambda f: rank[f["kind"]])  # failures first, then the rest in the page's order
    return {
        "flags": found,
        "new": sum(f["kind"] != "usual" for f in found),
        "compare": cmp,
        "passes": passes(rows, wk, now, dead),
        "chain": steps,
        "died": dead,
        "refusal_flag": REFUSAL_FLAG,
        "last_week": last["week"] if last else None,
    }
