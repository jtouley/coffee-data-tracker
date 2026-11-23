"""Week's crawl health (admin/health.py): the pass an attempt belongs to, a died attempt, a scheduled pass or connector
that never started, this week against last counted to each connector's latest finished pass, a rise in errors or
429s, the egress check's refusal share, deadlines (new or as last week), Tuesday's chain, and the page over the
synthetic lake (tests/admin_health_fixture.py)."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from admin import health, queries
from admin.config import Settings
from admin.main import create_app
from admin.store import FileStore
from coffeedata.connectors import CONNECTORS
from coffeedata.control import REFUSAL_FLAG
from coffeedata.fetchhealth import JOB_LIMIT
from coffeedata.lake import Lake

from .admin_health_fixture import NOW, W1, W2, W3, build_health_lake
from .test_admin import ORIGIN, SECRET, TOKEN

WEEK, LAST = "2026-11-23", "2026-11-16"


def at(h: int, m: int = 0, day: int = 0) -> datetime:
    return datetime(2026, 11, 23, tzinfo=UTC) + timedelta(days=day, hours=h, minutes=m)


def row(job="crawl", connector="generic", start=None, status="done", aid=None, week=WEEK, detail=None, arm=False,
        end=None, **counts):  # fmt: skip
    start = start or at(9, 19)
    return {"attempt_id": aid or f"{job}-{connector}-{start:%d%H%M%S}", "job": job, "connector": connector,
            "started_at": start, "ended_at": None if status == "running" else end or start + timedelta(minutes=20),
            "status": status, "arm": arm, "detail": detail or {}, "refusals": counts.pop("refusals", []),
            **{k: counts.get(k) for k in ("todo", "ok", "errors", "skipped", "http_429", "http_430")}}  # fmt: skip


def week_of(rows, week=WEEK, builds=()):
    return {"week": week, "rows": rows, "builds": list(builds)}


def codes(f, skip=("missed",)):
    """The flags' codes and subjects, leaving out (by default) the missed passes a hand-made week of one connector
    is bound to have."""
    return [
        (x["code"], x.get("connector") or x.get("job") or x.get("pass"))
        for x in f["flags"]
        if x["code"] not in skip
    ]


# Real-shaped weeks: the production 2026-11-16 and 2026-11-23 attempts' slots, statuses and counts (status, start,
# end, todo, ok, errors, 429), with made-up ids. `upto` cuts this week at a moment: attempts not started yet are
# left out, and those not ended yet are still running.
LAST_ROWS = [("shopify", 0, (9, 19), (9, 33), 1044, 1041, 3, 0), ("squarespace", 0, (9, 18), (9, 31), 323, 321, 2, 0),
             ("square_online", 0, (9, 18), (9, 24), 183, 182, 1, 0), ("woocommerce", 0, (9, 19), (10, 2), 228, 222, 7, 2),
             ("generic", 0, (9, 19), (9, 50), 661, 583, 84, 32), ("squarespace", 0, (15, 19), (15, 20), 1, 0, 1, 0),
             ("square_online", 0, (15, 19), (15, 20), 1, 0, 1, 0), ("generic", 0, (15, 20), (15, 34), 74, 6, 71, 26),
             ("woocommerce", 0, (15, 20), (15, 23), 9, 3, 6, 0), ("shopify", 0, (15, 20), (15, 22), 1, 0, 1, 0),
             ("square_online", 0, (21, 48), (21, 49), 1, 0, 1, 0), ("squarespace", 0, (21, 49), (21, 50), 1, 0, 1, 0),
             ("generic", 0, (21, 49), (22, 3), 68, 1, 71, 25), ("woocommerce", 0, (21, 49), (21, 52), 9, 3, 6, 2)]  # fmt: skip
THIS_ROWS = [("square_online", 0, (9, 19), (9, 25), 191, 184, 8, 0), ("squarespace", 0, (9, 19), (9, 33), 331, 322, 9, 0),
             ("woocommerce", 0, (9, 19), (9, 54), 245, 236, 13, 0), ("shopify", 0, (9, 19), (9, 34), 1062, 1059, 3, 0),
             ("generic", 0, (9, 20), (9, 50), 765, 689, 83, 10), ("square_online", 0, (15, 20), (15, 21), 7, 0, 7, 0),
             ("shopify", 0, (15, 20), (15, 22), 2, 0, 2, 0), ("squarespace", 0, (15, 20), (15, 22), 9, 0, 9, 0),
             ("woocommerce", 0, (15, 21), (15, 33), 12, 4, 10, 0), ("generic", 0, (15, 22), (15, 38), 75, 5, 74, 9)]  # fmt: skip
DEADLINE = {("woocommerce", (9, 19)), ("woocommerce", (15, 20)), ("woocommerce", (21, 49)),
            ("woocommerce", (15, 21)), ("generic", (9, 20)), ("generic", (15, 22))}  # fmt: skip


def real(spec, week, cut=None):
    days = 0 if week == WEEK else -7
    out = []
    for conn, _, (h, m), (eh, em), todo, ok, errors, h429 in spec:
        start, end = at(h, m, days), at(eh, em, days)
        if cut and start > cut:
            continue
        st = "running" if cut and end > cut else ("deadline" if (conn, (h, m)) in DEADLINE else "done")
        counts = {} if st == "running" else {"ok": ok, "errors": errors, "http_429": h429, "http_430": 0}
        if st == "deadline":  # the deadline left stores unstarted: a time limit under the truthful rule
            counts["skipped"] = 3
        out.append(row(connector=conn, start=start, end=end, status=st, week=week, todo=todo, **counts))
    return out


# --- passes and died attempts ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "start", "expect"),
    [
        ("crawl", at(9, 19), "morning"),
        ("crawl", at(9, 50), "morning"),  # Cloud Run's retry of the morning crawl
        ("crawl", at(15, 20), "resume"),
        ("crawl", at(21, 48), "retry"),
        ("crawl", at(2, day=1), "retry"),  # after midnight, before Tuesday's merge
        ("crawl-retry", at(21, 52), "retry"),
    ],
)
def test_an_attempt_belongs_to_the_pass_it_started_in(job, start, expect):
    assert health.slot(row(job, start=start), WEEK) == expect


def test_a_running_attempt_dies_past_the_time_limit_or_once_a_later_one_starts():
    live = row(start=at(21, 48), status="running", aid="live")
    old = row(connector="woocommerce", start=at(9, 19), status="running", aid="old")
    retried = row(start=at(15, 20), status="running", aid="retried")
    check = row("fetch-health", None, start=at(22, 0), status="running", aid="check")
    rows = [old, retried, live, check]
    assert health.died(rows, at(9, 19) + JOB_LIMIT - timedelta(minutes=1)) == {"retried"}
    assert health.died(rows, at(9, 19) + JOB_LIMIT) == {"retried", "old"}
    assert health.died(rows, at(22, 31)) == {
        "retried",
        "old",
        "check",
    }  # a fetch-health poll's limit is 30 min
    # a test crawl or a one-store run beside a pass is never its retry
    beside = [row(start=at(9, 19), status="running", aid="p"), row(start=at(10, 0), detail={"test": "t1"}),
              row(start=at(10, 5), detail={"only": "a.example"})]  # fmt: skip
    assert health.died(beside, at(11, 0)) == set()


def test_passes_say_what_each_tried_and_recovered_and_count_attempts_not_connectors():
    rows = [row(todo=765, ok=641, errors=131), row(connector="shopify", todo=1062, ok=1059, errors=3),
            row(start=at(15, 20), todo=124, ok=9, errors=115), row(start=at(15, 21), connector="woocommerce",
            status="deadline", todo=12, ok=4, errors=10, skipped=2),
            row(start=at(10, 0), todo=1, ok=1, errors=0, detail={"test": "t1"}),
            # a live attempt and, for woocommerce, Cloud Run's retry of one that died
            row(start=at(15, 30), connector="square_online", status="running", todo=50),
            row(start=at(15, 25), connector="woocommerce", status="running", todo=12, aid="dead"),
            row(start=at(15, 40), connector="woocommerce", status="running", todo=12, aid="again")]  # fmt: skip
    p = {x["key"]: x for x in health.passes(rows, WEEK, at(20, 0), {"dead"})}
    assert (p["morning"]["tried"], p["morning"]["ok"], p["morning"]["status"]) == (
        1827,
        1700,
        "done",
    )  # no test
    # the running attempts' stores aren't counted until their end rows say how they went
    assert (p["resume"]["tried"], p["resume"]["ok"], p["resume"]["status"]) == (136, 13, "died")
    assert (p["resume"]["running"], p["resume"]["died"], len(p["resume"]["connectors"])) == (2, 1, 3)
    assert p["retry"]["status"] == "not yet" and p["retry"]["tried"] is None
    assert {x["key"]: x for x in health.passes(rows, WEEK, at(22, 30), set())}["retry"][
        "status"
    ] == "didn't run"


# --- missed runs (the review's removed-row cases) -------------------------------------------------------------------


def _full_week():
    """Every connector in every pass, done (Shopify's retry as the crawl-retry job)."""
    out = []
    for i, (key, _, t) in enumerate(health.PASSES):
        for name in CONNECTORS:
            job = "crawl-retry" if (name, key) == ("shopify", "retry") else "crawl"
            out.append(row(job, name, start=at(t.hour, t.minute + 2), todo=10 - i, ok=1, errors=9 - i))
    return out


def test_a_full_week_misses_nothing_and_a_pass_is_due_only_after_its_grace():
    assert health.missed(_full_week(), WEEK, at(23, 30)) == []
    morning = [r for r in _full_week() if health.slot(r, WEEK) == "morning"]
    assert health.missed(morning, WEEK, at(15, 17) + health.MISSED_AFTER - timedelta(minutes=1)) == []
    assert [m["pass"] for m in health.missed(morning, WEEK, at(15, 17) + health.MISSED_AFTER)] == ["resume"]


def test_a_whole_pass_that_never_started_is_flagged():
    rows = [r for r in _full_week() if health.slot(r, WEEK) != "resume"]
    f = health.flags(week_of(rows), None, None, at(23, 30))
    (m,) = [x for x in f["flags"] if x["code"] == "missed"]
    assert (m["pass"], m["whole"], m["kind"], m["connectors"]) == ("resume", True, "bad", list(CONNECTORS))
    assert {p["key"]: p["status"] for p in f["passes"]}["resume"] == "didn't run"


def test_one_connectors_missing_pass_is_flagged():
    rows = [
        r for r in _full_week() if not (r["connector"] == "woocommerce" and health.slot(r, WEEK) == "resume")
    ]
    f = health.flags(week_of(rows), None, None, at(23, 30))
    assert [(x["pass"], x["connectors"], x["whole"]) for x in f["flags"] if x["code"] == "missed"] == [
        ("resume", ["woocommerce"], False)
    ]


def test_a_connector_absent_all_week_is_flagged_and_never_reads_as_an_improvement():
    last = [r | {"week": LAST, "started_at": r["started_at"] - timedelta(days=7)} for r in _full_week()]
    rows = [r for r in _full_week() if r["connector"] != "woocommerce"]
    f = health.flags(week_of(rows), week_of(last, LAST), None, at(23, 30))
    # one line for its week, not one per pass
    assert [(x["code"], x["connectors"]) for x in f["flags"]] == [("absent", ["woocommerce"])]
    assert next(x for x in f["flags"])["passes"] == ["morning", "resume", "retry"]
    woo = next(r for r in f["compare"]["rows"] if r["connector"] == "woocommerce")
    assert (woo["status"], woo["this"]) == ("didn't run", None) and set(woo["delta"].values()) == {None}
    # the All row compares only connectors with both weeks: no −9 from woocommerce's missing week
    assert f["compare"]["total"]["delta"]["errors"] == 0


# --- this week against last ----------------------------------------------------------------------------------------


def test_this_week_is_compared_with_last_week_counted_to_the_same_pass():
    last = [row(week=LAST, start=at(9, 19, -7), todo=661, ok=583, errors=84, http_429=32,
                refusals=[{"ip": "203.0.113.40", "stores": 661, "refused": 28}]),
            row(week=LAST, start=at(15, 20, -7), todo=74, ok=6, errors=71, http_429=26),
            row(week=LAST, start=at(21, 48, -7), todo=68, ok=1, errors=71, http_429=25)]  # fmt: skip
    this = [row(todo=765, ok=641, errors=131, http_429=140,
                refusals=[{"ip": "203.0.113.40", "stores": 765, "refused": 91}])]  # fmt: skip
    c = health.compare(this, last, WEEK, LAST)
    assert c["upto"] == "morning"
    (g,) = c["rows"]
    assert (
        g["last"]["errors"] == 84 and g["last"]["http_429"] == 32 and g["last"]["ok"] == 583
    )  # morning only
    assert g["delta"] == {
        "ok": 58,
        "errors": 47,
        "http_429": 108,
        "http_430": 0,
        "refused": 63,
        "stores": 104,
    }
    assert c["total"]["this"]["ok"] == 641 and c["total"]["delta"]["errors"] == 47
    this.append(row(start=at(15, 20), todo=124, ok=9, errors=115, http_429=3))
    (g,) = health.compare(this, last, WEEK, LAST)["rows"]
    # ok is summed over passes (each tries only what's still failing); errors are the latest pass's
    assert (g["upto"], g["this"]["ok"], g["this"]["errors"], g["last"]["errors"]) == ("resume", 650, 115, 71)


def test_shopifys_errors_include_its_evening_retry():
    rows = [row(connector="shopify", todo=1062, ok=1059, errors=3),
            row("crawl-retry", "shopify", start=at(21, 52), todo=5, ok=2, errors=3)]  # fmt: skip
    (s,) = health.compare(rows, [], WEEK, LAST)["rows"]
    assert (s["upto"], s["this"]["ok"], s["this"]["errors"]) == ("retry", 1061, 6)


def test_mid_morning_a_running_connector_is_running_not_zeros_minus_last_week():
    """The real week at 09:40 UTC: generic and woocommerce still crawling, squarespace too."""
    this, last = real(THIS_ROWS, WEEK, at(9, 40)), real(LAST_ROWS, LAST)
    f = health.flags(week_of(this), week_of(last, LAST), None, at(9, 40))
    by = {r["connector"]: r for r in f["compare"]["rows"]}
    for name in ("generic", "woocommerce"):
        assert (by[name]["status"], by[name]["this"], by[name]["delta"]["errors"]) == ("running", None, None)
    assert by["shopify"]["delta"]["errors"] == 0 and by["square_online"]["delta"]["errors"] == 7
    assert f["compare"]["total"]["partial"]
    # the finished connectors' real rise (1 or 2 errors to 8 or 9) shows; the running ones get no rise check
    assert codes(f, skip=()) == [("jump", "squarespace"), ("jump", "square_online")]


def test_mid_resume_compares_finished_connectors_like_for_like_and_skips_running_ones():
    """The real week at 15:25 UTC: the resume pass running for generic and woocommerce."""
    this, last = real(THIS_ROWS, WEEK, at(15, 25)), real(LAST_ROWS, LAST)
    f = health.flags(week_of(this), week_of(last, LAST), None, at(15, 25))
    by = {r["connector"]: r for r in f["compare"]["rows"]}
    woo = by["woocommerce"]  # counted to its morning, both weeks, but running: no change and no rise
    assert (woo["status"], woo["upto"], woo["this"]["errors"], woo["delta"]["errors"]) == (
        "running",
        "morning",
        13,
        None,
    )
    sq = by["squarespace"]  # its resume ended: compared with last week's resume
    assert (sq["status"], sq["upto"], sq["this"]["errors"], sq["last"]["errors"]) == (None, "resume", 9, 1)
    assert {x["connector"] for x in f["flags"] if x["code"] == "jump"} == {"squarespace", "square_online"}


def test_a_connector_with_no_crawl_last_week_has_no_change():
    c = health.compare([row(connector="shopify", todo=5, ok=5, errors=0)], [], WEEK, LAST)
    assert c["rows"][0]["delta"] == dict.fromkeys(health.FIELDS)
    assert c["total"]["delta"]["ok"] is None


@pytest.mark.parametrize(
    ("this", "last", "up"),
    [
        (131, 71, True),
        (84, 83, False),
        (5, 0, True),
        (4, 0, False),
        (3, 3, False),
        (10, 32, False),
        (115, 84, False),
    ],
)
def test_a_jump_is_more_than_four_standard_errors_of_last_week(this, last, up):
    assert health.jumped(this, last) is up


# --- flags -----------------------------------------------------------------------------------------------------------


def test_refusals_over_the_egress_check_share_are_flagged_and_twice_is_said():
    def crawl(week, start, stores, refused):
        return row(week=week, start=start, todo=stores, ok=stores, errors=0,
                   refusals=[{"ip": "203.0.113.40", "stores": stores, "refused": refused}])  # fmt: skip

    over = crawl(WEEK, at(9, 19), 1000, 91)
    under = crawl(LAST, at(9, 19, -7), 1000, 80)  # exactly 8%: not over
    f = health.flags(week_of([over]), week_of([under], LAST), None, at(10, 0))
    (r,) = [x for x in f["flags"] if x["code"] == "refusals"]
    assert r["share"] == pytest.approx(0.091) and not r["twice"] and REFUSAL_FLAG == 0.08
    f = health.flags(week_of([over]), week_of([crawl(LAST, at(9, 19, -7), 1000, 81)], LAST), None, at(10, 0))
    assert [x["twice"] for x in f["flags"] if x["code"] == "refusals"] == [True]
    assert not [
        x for x in health.flags(week_of([under]), None, None, at(10, 0))["flags"] if x["code"] == "refusals"
    ]


def test_flags_a_failed_or_died_attempt_a_deadline_and_a_jump_but_not_a_test_crawl():
    rows = [row(todo=765, ok=641, errors=131, http_429=140), row(connector="woocommerce", status="deadline", todo=9, skipped=2),
            row(connector="woocommerce", start=at(15, 20), status="deadline", todo=9, skipped=2),
            row(start=at(15, 20), status="running", aid="dead"), row(start=at(21, 48), todo=60, ok=2, errors=58),
            row("fetch-health", None, start=at(15, 47), status="failed"),
            row(start=at(11, 0), status="failed", detail={"test": "t1"})]  # fmt: skip
    last = [row(week=LAST, start=at(9, 19, -7), todo=661, ok=583, errors=30, http_429=32)]
    f = health.flags(week_of(rows), week_of(last, LAST), None, at(23, 30))
    got = codes(f)
    assert got[:2] == [("died", "generic"), ("failed", "fetch-health")]  # failures first
    assert ("deadline", "woocommerce") in got
    (jump,) = [x for x in f["flags"] if x["code"] == "jump"]  # one line per connector
    assert [(u["field"], u["last"], u["this"]) for u in jump["ups"]] == [
        ("errors", 30, 58),
        ("http_429", 32, 140),
    ]
    assert next(x for x in f["flags"] if x["code"] == "died")["pass"] == "resume"
    assert (
        f["died"] == {"dead"} and len([c for c in got if c[0] == "failed"]) == 1
    )  # the test crawl's isn't here


def test_a_deadline_as_last_week_is_usual_and_the_last_pass_says_its_stores_went_uncrawled():
    def woo(week, *slots):
        days = 0 if week == WEEK else -7
        return [row(connector="woocommerce", week=week, status="deadline", start=at(h, m, days), todo=9,
                    detail={"stopped_deadline": 1})
                for h, m in slots]  # fmt: skip

    every = ((9, 19), (15, 20), (21, 48))
    f = health.flags(week_of(woo(WEEK, *every)), week_of(woo(LAST, *every), LAST), None, at(23, 30))
    (d,) = [x for x in f["flags"] if x["code"] == "deadline"]
    assert (d["kind"], d["final"], d["last_passes"]) == ("usual", True, ["morning", "resume", "retry"])
    assert f["new"] == len(f["flags"]) - 1  # a usual line isn't counted as something new to look at
    worse = health.flags(week_of(woo(WEEK, *every)), week_of(woo(LAST, (9, 19)), LAST), None, at(23, 30))
    assert [x["kind"] for x in worse["flags"] if x["code"] == "deadline"] == ["warn"]
    first = health.flags(week_of(woo(WEEK, (9, 19))), None, None, at(12, 0))
    assert [(x["kind"], x["final"]) for x in first["flags"] if x["code"] == "deadline"] == [("warn", False)]


def test_tuesday_is_not_missing_until_its_window_ends_and_only_the_first_gap_is_flagged():
    crawl = _full_week()
    monday_night = health.flags(week_of(crawl), None, None, at(23, 0))
    assert [c["status"] for c in monday_night["chain"]] == ["not yet"] * 4
    assert not monday_night["flags"]
    rows = crawl + [row("merge", None, start=at(3, 18, 1))]
    later = health.flags(week_of(rows), None, None, at(13, 0, 1))
    assert [(c["job"], c["status"]) for c in later["chain"]] == [
        ("merge", "done"), ("enrich", "missing"), ("build", "missing"), ("publish", "missing")]  # fmt: skip
    assert codes(later) == [("chain_missing", "enrich")]


def test_a_failed_build_is_one_flag_and_a_served_build_is_published():
    rows = [*_full_week(), row("merge", None, start=at(3, 18, 1)), row("enrich", None, start=at(4, 18, 1)),
            row("build", None, start=at(6, 18, 1), status="failed")]  # fmt: skip
    b = {"build_id": "B", "passed": False, "built_at": at(6, 40, 1), "served_at": None}
    f = health.flags(week_of(rows, builds=[b]), None, None, at(13, 0, 1))
    assert [x["code"] for x in f["flags"]] == ["build_failed"]
    assert [c["status"] for c in f["chain"]][-2:] == ["failed", "not published"]
    ok = {"build_id": "B", "passed": True, "built_at": at(6, 40, 1), "served_at": at(6, 44, 1)}
    f = health.flags(week_of(rows[:-1], builds=[ok]), None, {"run_date": "2026-11-30"}, at(13, 0, 1))
    assert f["chain"][-1] == {"job": "publish", "status": "published", "at": at(6, 44, 1), "build_id": "B"}


# --- over the synthetic lake ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hlake(tmp_path_factory):
    root = tmp_path_factory.mktemp("health") / "lake"
    build_health_lake(root)
    return Lake(str(root), read_only=True)


def _read(lake, fn, *a):
    return fn(lake, lake.connect(), *a)


def test_the_synthetic_weeks_flag_what_they_were_made_to(hlake):
    this, last = _read(hlake, queries.week, W3), _read(hlake, queries.week, W2)
    served = _read(hlake, queries.served)
    f = health.flags(this, last, served, NOW)
    assert [(x["code"], x["kind"]) for x in f["flags"]][:3] == [
        ("died", "bad"),
        ("failed", "bad"),
        ("missed", "bad"),
    ]
    assert sorted(codes(f, skip=())) == [
        ("deadline", "generic"), ("deadline", "woocommerce"), ("died", "generic"), ("failed", "fetch-health"),
        ("jump", "generic"), ("jump", "squarespace"), ("missed", "retry"), ("partial", "squarespace"),
        ("refusals", None)]  # fmt: skip
    assert (
        next(x for x in f["flags"] if x["code"] == "deadline" and x["connector"] == "woocommerce")["kind"]
        == "usual"
    )
    assert next(x for x in f["flags"] if x["code"] == "missed")["connectors"] == ["woocommerce"]
    assert f["new"] == 7
    ups = {x["connector"]: [u["field"] for u in x["ups"]] for x in f["flags"] if x["code"] == "jump"}
    assert ups == {
        "generic": ["errors", "http_429"],
        "squarespace": ["errors"],
    }  # square_online: still running
    assert [p["status"] for p in f["passes"]] == ["deadline", "died", "running"]
    clean = health.flags(_read(hlake, queries.week, W1), None, served, NOW)
    assert clean["flags"] == [] and [c["status"] for c in clean["chain"]] == [
        "done",
        "done",
        "passed",
        "published",
    ]


def test_stores_that_keep_failing_count_weeks_in_a_row(hlake):
    s = _read(hlake, queries.failing_streaks, W3)
    assert [(r["roaster_id"], r["weeks"], r["status"]) for r in s[:4]] == [
        ("always-down.example", 4, "error"), ("half-catalog.example", 3, "partial"),
        ("blocked-since.example", 2, "error"), ("slow-00.example", 2, "error")]  # fmt: skip
    assert len(s) == 27 and "one-bad-week.example" not in {r["roaster_id"] for r in s}
    assert _read(hlake, queries.failing_streaks, W1)[0]["roaster_id"] == "always-down.example"


def _client(tmp_path, lake, now=NOW):
    st = Settings(origin=ORIGIN, session_secret=SECRET, enroll_token=TOKEN, lake_url=str(lake.root),
                  cache_dir=tmp_path / "cache", store=FileStore(tmp_path / "state")).check()  # fmt: skip
    app = create_app(st, now=lambda: now)
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("c"))
    return c


def test_week_answers_first_then_says_what_needs_a_look(tmp_path, hlake):
    text = _client(tmp_path, hlake).get(f"/part/week?week={W3}").text
    answer = text[text.index("data-answer") : text.index("</p>", text.index("data-answer"))]
    assert "5 of 5 connectors landed" in answer and "7 things to look at" in answer
    assert "Tuesday’s merge hasn’t run yet" in answer
    for words in ("generic’s resume crawl died.", "never reported finishing", "No retry crawl for woocommerce.",
                  "woocommerce hit its time limit</strong> in the morning and resume passes, as last week. The stores it didn’t reach or finish weren’t read whole this week.",
                  "squarespace read 2 catalogs only in part</strong> in its retry pass. The products it didn’t read stay listed as they were. big-catalog.example was partial in each of its last 2 runs, so not read whole in either.",
                  "egress check flags an IP that is over on two runs in a row", "up more than usual:",
                  "(1 still running)", "(1 died)"):  # fmt: skip
        assert words in text, words
    assert "never finished" not in text
    assert "Failing week after week (27)" in text and "Page 1 of 3" in text
    assert "action=crawl-one" not in text  # writes are off: no action that would only say so
    clean = _client(tmp_path / "w1", hlake).get(f"/part/week?week={W1}").text
    assert "nothing unusual · published" in clean and 'id="look"' not in clean


def test_with_writes_on_each_store_that_keeps_failing_has_its_test_crawl(tmp_path, hlake, pg_url):
    from admin.controlplane import ControlPlane, url_connect

    from .admin_fakes import FakeJobs

    st = Settings(origin=ORIGIN, session_secret=SECRET, enroll_token=TOKEN, lake_url=str(hlake.root),
                  cache_dir=tmp_path / "cache", store=FileStore(tmp_path / "state"),
                  control=ControlPlane(url_connect(pg_url)), jobs=FakeJobs(),
                  snapshots=FileStore(tmp_path / "snapshots")).check()  # fmt: skip
    app = create_app(st, now=lambda: NOW)
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("cred-abcdefgh"))
    text = c.get(f"/part/week?week={W3}").text
    keep = text[text.index('id="keep-failing"') : text.index("Stores (")]
    assert keep.count("action=crawl-one") == 10  # a page of them
    assert "connector=generic&amp;roaster=always-down.example" in keep


def test_the_passes_are_the_crawl_schedules_in_terraform():
    """Every crawl schedule in ingestion.tf is one of PASSES' times, and each pass schedules every connector's job
    (Shopify's 21:47 as crawl-retry): what `missed` expects."""
    tf = (Path(__file__).parents[1] / "src/terraform/ingestion.tf").read_text()
    jobs: dict[tuple[str, str], set[str]] = {}
    for job, m, h in re.findall(r'job = "(crawl-[a-z-]+)", cron = "(\d+) (\d+) \* \* 1"', tf):
        jobs.setdefault((m, h), set()).add(job)
    assert set(jobs) == {(str(t.minute), str(t.hour)) for _, _, t in health.PASSES}
    every = {"crawl-" + c.replace("_", "-") for c in CONNECTORS}
    for key, _, t in health.PASSES:
        assert health.slot(row(start=at(t.hour, t.minute)), WEEK) == key
        expect = every - {"crawl-shopify"} | {"crawl-retry"} if key == "retry" else every
        assert jobs[(str(t.minute), str(t.hour))] == expect


# --- the final review's fixes ----------------------------------------------------------------------------------------


def test_a_pass_whose_attempts_all_died_shows_no_numbers_and_no_change():
    """generic's real morning, replayed as died only (Cloud Run killed it, no end row), seen at 14:00."""
    last = real(LAST_ROWS, LAST)
    this = [r if r["connector"] != "generic" else r | {"status": "running", "ok": None, "errors": None}
            for r in real(THIS_ROWS, WEEK, at(9, 59))]  # fmt: skip
    f = health.flags(week_of(this), week_of(last, LAST), None, at(14, 0))
    g = next(r for r in f["compare"]["rows"] if r["connector"] == "generic")
    assert (g["status"], g["this"], g["delta"]["errors"]) == ("died", None, None)
    assert "generic" not in {x.get("connector") for x in f["flags"] if x["code"] == "jump"}
    assert ("died", "generic") in codes(f)
    # its morning ended, its resume only died: counted to the morning, with no change while that's the latest
    this = real(THIS_ROWS, WEEK, at(15, 21)) + [row(start=at(15, 22), status="running", aid="r")]
    f = health.flags(week_of(this), week_of(last, LAST), None, at(15, 22) + JOB_LIMIT)
    g = next(r for r in f["compare"]["rows"] if r["connector"] == "generic")
    assert (g["status"], g["upto"], g["this"]["errors"], g["delta"]["errors"]) == (
        "died",
        "morning",
        83,
        None,
    )


def test_a_shopify_deadline_before_its_retry_says_its_stores_went_uncrawled():
    """Shopify's 21:47 pass is crawl-retry, which takes only parked stores: it never picks up the rest."""
    rows = [r | {"status": "deadline", "skipped": 2}
            if (r["connector"], health.slot(r, WEEK)) == ("shopify", "resume") else r
            for r in _full_week()]  # fmt: skip
    for now in (at(16, 0), at(23, 30)):  # before the retry, and after it ran
        (d,) = [x for x in health.flags(week_of(rows), None, None, now)["flags"] if x["code"] == "deadline"]
        assert (d["connector"], d["final"]) == ("shopify", True)
    woo = [r | {"status": "deadline", "skipped": 2}
           if (r["connector"], health.slot(r, WEEK)) == ("woocommerce", "resume") else r
           for r in _full_week()]  # fmt: skip
    (d,) = [x for x in health.flags(week_of(woo), None, None, at(16, 0))["flags"] if x["code"] == "deadline"]
    assert not d["final"]  # woocommerce's own 21:47 crawl takes them


def test_a_test_crawl_is_never_marked_died_because_a_pass_started_after_it():
    rows = [row(start=at(9, 10), status="running", aid="t", detail={"test": "t1"}), row(start=at(9, 19))]
    assert health.died(rows, at(10, 0)) == set()


def test_weeks_in_a_row_says_when_it_is_the_whole_window(hlake):
    s = {r["roaster_id"]: r for r in _read(hlake, queries.failing_streaks, W3, 4)}
    assert (s["always-down.example"]["weeks"], s["always-down.example"]["capped"]) == (4, True)
    assert not s["half-catalog.example"]["capped"]
    assert not any(
        r["capped"] for r in _read(hlake, queries.failing_streaks, W3)
    )  # 4 weeks of fetches, under 8


def test_last_week_or_the_streaks_failing_costs_only_their_own_block(tmp_path, hlake, monkeypatch):
    real_week = queries.week

    def broken_last_week(lake, con, run_date):
        if run_date == W2:
            raise OSError("HTTP 503 from the lake")
        return real_week(lake, con, run_date)

    def broken_streaks(*a):
        raise OSError("HTTP 503 from the lake")

    monkeypatch.setattr(queries, "week", broken_last_week)
    monkeypatch.setattr(queries, "failing_streaks", broken_streaks)
    r = _client(tmp_path, hlake).get(f"/part/week?week={W3}")
    assert r.status_code == 200
    assert "Couldn’t read last week (OSError: HTTP 503 from the lake)" in r.text
    assert "Couldn’t read the stores that keep failing: OSError: HTTP 503 from the lake" in r.text
    assert "No retry crawl for woocommerce." in r.text and "Every job attempt" in r.text


def test_partial_catalogs_are_a_calm_line_of_their_own_never_a_time_limit():
    """Since the crawl's end status is truthful, a pass whose catalogs were read in part for another reason (a count
    that disagrees, a failed page) ends `done`: Week lists its partials calmly, from its latest ended pass (each pass
    asks a partial store again, so passes aren't summed), and says "hit its time limit" only for a `deadline` row."""
    part = {"partial": 3, "partial_every_week": ["b.example", "c.example"]}
    rows = [row(connector="woocommerce", todo=245, ok=236, errors=13, detail=part),
            row(connector="woocommerce", start=at(15, 20), todo=12, ok=4, errors=10,
                detail={"partial": 2, "partial_every_week": ["b.example"]}),
            row(connector="generic", start=at(9, 20), status="deadline", todo=765,
                detail={"partial": 2, "stopped_deadline": 2}),
            row(connector="squarespace", start=at(9, 21), detail={"partial": 0, "partial_every_week": []}),
            row(connector="shopify", start=at(9, 22), status="running", detail=part)]  # fmt: skip
    f = health.flags(week_of(rows), None, None, at(16, 0))
    got = [(x["code"], x["connector"], x["kind"]) for x in f["flags"] if x["code"] in ("deadline", "partial")]
    assert got == [("deadline", "generic", "warn"), ("partial", "woocommerce", "usual"),
                   ("partial", "generic", "usual")]  # fmt: skip
    woo = next(x for x in f["flags"] if x["code"] == "partial" and x["connector"] == "woocommerce")
    assert (woo["pass"], woo["partial"], woo["every_week"], woo["weeks"]) == ("resume", 2, ["b.example"], 2)
    # a later pass that didn't ask the partial stores again (detail.partial 0) doesn't hide them
    later = rows + [
        row(connector="woocommerce", start=at(21, 48), todo=8, ok=0, errors=8, detail={"partial": 0})
    ]
    f2 = health.flags(week_of(later), None, None, at(23, 0))
    woo = next(x for x in f2["flags"] if x["code"] == "partial" and x["connector"] == "woocommerce")
    assert (woo["pass"], woo["partial"]) == ("resume", 2)
    assert f["new"] == sum(x["kind"] != "usual" for x in f["flags"])  # partials are never counted as new
    assert not any(x["code"] == "deadline" and x["connector"] == "woocommerce" for x in f["flags"])


def test_week_words_a_time_limit_and_partial_catalogs(tmp_path):
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "admin/templates")))
    src = env.loader.get_source(env, "part_week.html")[0]
    start = src.index("{% macro listed")
    macros = src[start : src.index("{% macro cell")]
    tpl = env.from_string(macros + "{{ flag(f) }}")

    def say(f):
        return re.sub(r"<[^>]+>", "", tpl.render(f=f, n=lambda v: f"{v:,}", t=str, health={})).strip()

    assert say(
        {
            "code": "deadline",
            "kind": "warn",
            "connector": "generic",
            "passes": ["morning"],
            "last_passes": [],
            "final": False,
        }
    ) == (  # fmt: skip
        "generic hit its time limit in the morning pass. The next pass picks up the stores it didn’t reach or finish."
    )
    assert say(
        {
            "code": "partial",
            "kind": "usual",
            "connector": "woocommerce",
            "pass": "morning",
            "partial": 1,
            "every_week": [],
            "weeks": 2,
        }
    ) == (  # fmt: skip
        "woocommerce read 1 catalog only in part in its morning pass. The products it didn’t read stay listed as they "
        "were."
    )
    many = [f"s{i}.example" for i in range(7)]
    assert say(
        {
            "code": "partial",
            "kind": "usual",
            "connector": "generic",
            "pass": "retry",
            "partial": 9,
            "every_week": many,
            "weeks": 2,
        }
    ).endswith(  # fmt: skip
        "s0.example, s1.example, s2.example, s3.example, s4.example and 2 more were partial in each of their last 2 "
        "runs, so not read whole in either."
    )


def test_an_old_partial_only_deadline_row_is_no_time_limit_this_week_or_as_last_week():
    """Rows written under the old rule said `deadline` for any partial catalog, with `skipped` 0 and none of the
    deadline's counts in detail. They read as finished passes: no "hit its time limit" line, a `done` pass, and no
    "as last week" that would make this week's real time limit read as usual."""

    def woo(week, status="deadline", **kw):
        days = 0 if week == WEEK else -7
        return [row(connector="woocommerce", week=week, status=status, start=at(h, m, days), todo=9, skipped=0, **kw)
                for h, m in ((9, 19), (15, 20), (21, 48))]  # fmt: skip

    old = woo(LAST, detail={"partial": 3, "partial_every_week": ["b.example"]})
    assert not any(health.timed_out(r) for r in old)
    f = health.flags(week_of(old, LAST), None, None, at(23, 30, -7))
    assert not [x for x in f["flags"] if x["code"] == "deadline"]
    assert [p["status"] for p in f["passes"]] == ["done", "done", "done"]
    assert [(x["code"], x["kind"]) for x in f["flags"] if x["code"] == "partial"] == [("partial", "usual")]
    # this week the deadline really stopped a catalog: new, not "as last week"
    real_stop = woo(WEEK, detail={"partial": 3, "stopped_deadline": 1})
    f = health.flags(week_of(real_stop), week_of(old, LAST), None, at(23, 30))
    (d,) = [x for x in f["flags"] if x["code"] == "deadline"]
    assert (d["kind"], d["last_passes"]) == ("warn", [])
    # an old row with stores skipped was a time limit, and still reads as one
    assert health.timed_out(row(connector="woocommerce", status="deadline", skipped=4))
    for k in ("stopped_deadline", "parked_at_deadline", "not_retried_at_deadline"):
        assert health.timed_out(row(connector="woocommerce", status="deadline", skipped=0, detail={k: 1}))
    assert not health.timed_out(row(connector="woocommerce", status="done", skipped=0, detail={"partial": 2}))
