"""Week's crawl health (admin/health.py): the pass an attempt belongs to, a died attempt, this week against last
counted to the same pass, a jump in errors or 429s, the egress check's refusal share, Tuesday's chain, and the page
over the synthetic lake (tests/admin_health_fixture.py)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from admin import health, queries
from admin.config import Settings
from admin.main import create_app
from admin.store import FileStore
from coffeedata.control import REFUSAL_FLAG
from coffeedata.fetchhealth import JOB_LIMIT
from coffeedata.lake import Lake

from .admin_health_fixture import NOW, W1, W2, W3, build_health_lake
from .test_admin import ORIGIN, SECRET, TOKEN

WEEK = "2026-11-23"


def at(h: int, m: int = 0, day: int = 0) -> datetime:
    return datetime(2026, 11, 23, tzinfo=UTC) + timedelta(days=day, hours=h, minutes=m)


def row(job="crawl", connector="generic", start=None, status="done", aid=None, week=WEEK, detail=None, arm=False,
        **counts):  # fmt: skip
    start = start or at(9, 19)
    return {"attempt_id": aid or f"{job}-{connector}-{start:%d%H%M}", "job": job, "connector": connector,
            "started_at": start, "ended_at": None if status == "running" else start + timedelta(minutes=20),
            "status": status, "arm": arm, "detail": detail or {}, "refusals": counts.pop("refusals", []),
            **{k: counts.get(k) for k in ("todo", "ok", "errors", "http_429", "http_430")}}  # fmt: skip


def week_of(rows, week=WEEK, builds=()):
    return {"week": week, "rows": rows, "builds": list(builds)}


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


def test_passes_say_what_each_tried_and_recovered():
    rows = [row(todo=765, ok=641, errors=131), row(connector="shopify", todo=1062, ok=1059, errors=3),
            row(start=at(15, 20), todo=124, ok=9, errors=115), row(start=at(15, 21), connector="woocommerce",
            status="deadline", todo=12, ok=4, errors=10),
            row(start=at(10, 0), todo=1, ok=1, errors=0, detail={"test": "t1"}),
            row(start=at(15, 30), connector="square_online", status="running", todo=50)]  # fmt: skip
    p = {x["key"]: x for x in health.passes(rows, WEEK, at(20, 0), set())}
    assert (p["morning"]["tried"], p["morning"]["ok"], p["morning"]["status"]) == (
        1827,
        1700,
        "done",
    )  # no test
    # the running attempt's 50 aren't counted until its end row says how they went
    assert (p["resume"]["tried"], p["resume"]["ok"], p["resume"]["status"]) == (136, 13, "deadline")
    assert (p["resume"]["ended"], len(p["resume"]["connectors"])) == (2, 3)
    assert p["retry"]["status"] == "not yet" and p["retry"]["tried"] is None
    assert {x["key"]: x for x in health.passes(rows, WEEK, at(22, 0), set())}["retry"][
        "status"
    ] == "didn't run"


# --- this week against last --------------------------------------------------------------------------------------


def test_this_week_is_compared_with_last_week_counted_to_the_same_pass():
    last = [row(week="2026-11-16", start=at(9, 19, -7), todo=661, ok=583, errors=84, http_429=32,
                refusals=[{"ip": "203.0.113.40", "stores": 661, "refused": 28}]),
            row(week="2026-11-16", start=at(15, 20, -7), todo=74, ok=6, errors=71, http_429=26),
            row(week="2026-11-16", start=at(21, 48, -7), todo=68, ok=1, errors=71, http_429=25)]  # fmt: skip
    this = [row(todo=765, ok=641, errors=131, http_429=140,
                refusals=[{"ip": "203.0.113.40", "stores": 765, "refused": 91}])]  # fmt: skip
    c = health.compare(this, last, WEEK, "2026-11-16")
    assert c["upto"] == "morning"
    g = next(r for r in c["rows"] if r["connector"] == "generic")
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
    c = health.compare(this, last, WEEK, "2026-11-16")
    g = next(r for r in c["rows"] if r["connector"] == "generic")
    # ok is summed over passes (each tries only what's still failing); errors are the latest pass's
    assert (c["upto"], g["this"]["ok"], g["this"]["errors"], g["last"]["errors"]) == ("resume", 650, 115, 71)


def test_a_connector_with_no_crawl_last_week_has_no_change():
    c = health.compare([row(connector="shopify", todo=5, ok=5, errors=0)], [], WEEK, "2026-11-16")
    assert c["rows"][0]["delta"] == dict.fromkeys(health.FIELDS)
    assert c["total"]["last"] is None and c["total"]["delta"]["ok"] is None


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


def test_refusals_over_the_egress_check_share_are_flagged_and_twice_is_what_fails_it():
    def crawl(week, start, stores, refused):
        return row(week=week, start=start, todo=stores, ok=stores, errors=0,
                   refusals=[{"ip": "203.0.113.40", "stores": stores, "refused": refused}])  # fmt: skip

    over = crawl(WEEK, at(9, 19), 1000, 91)
    under = crawl("2026-11-16", at(9, 19, -7), 1000, 80)  # exactly 8%: not over
    f = health.flags(week_of([over]), week_of([under], "2026-11-16"), None, at(20, 0))
    (r,) = [x for x in f["flags"] if x["code"] == "refusals"]
    assert r["share"] == pytest.approx(0.091) and not r["twice"] and REFUSAL_FLAG == 0.08
    f = health.flags(week_of([over]), week_of([crawl("2026-11-16", at(9, 19, -7), 1000, 81)], "2026-11-16"), None,
                     at(20, 0))  # fmt: skip
    assert [x["twice"] for x in f["flags"] if x["code"] == "refusals"] == [True]
    assert not [
        x for x in health.flags(week_of([under]), None, None, at(20, 0))["flags"] if x["code"] == "refusals"
    ]


def test_flags_a_failed_or_died_attempt_a_deadline_and_a_jump_but_not_a_test_crawl():
    rows = [row(todo=765, ok=641, errors=131, http_429=140), row(connector="woocommerce", status="deadline", todo=9),
            row(connector="woocommerce", start=at(15, 20), status="deadline", todo=9),
            row(start=at(15, 20), status="running", aid="dead"), row(start=at(21, 48), status="running"),
            row("fetch-health", None, start=at(15, 47), status="failed"),
            row(start=at(11, 0), status="failed", detail={"test": "t1"})]  # fmt: skip
    last = [row(week="2026-11-16", start=at(9, 19, -7), todo=661, ok=583, errors=84, http_429=32)]
    f = health.flags(week_of(rows), week_of(last, "2026-11-16"), None, at(23, 30))
    codes = [(x["code"], x.get("connector") or x.get("job")) for x in f["flags"]]
    assert codes[:2] == [("died", "generic"), ("failed", "fetch-health")]  # failures first
    assert ("deadline", "woocommerce") in codes
    (jump,) = [x for x in f["flags"] if x["code"] == "jump"]  # one line per connector
    assert [(u["field"], u["last"], u["this"]) for u in jump["ups"]] == [
        ("errors", 84, 131),
        ("http_429", 32, 140),
    ]
    assert next(x for x in f["flags"] if x["code"] == "deadline")["passes"] == ["morning", "resume"]
    assert next(x for x in f["flags"] if x["code"] == "died")["pass"] == "resume"
    assert (
        f["died"] == {"dead"} and len([c for c in codes if c[0] == "failed"]) == 1
    )  # the test crawl's isn't here


def test_tuesday_is_not_missing_until_its_window_ends_and_only_the_first_gap_is_flagged():
    crawl = [row(todo=10, ok=10, errors=0)]
    monday_night = health.flags(week_of(crawl), None, None, at(23, 0))
    assert [c["status"] for c in monday_night["chain"]] == ["not yet"] * 4
    assert not monday_night["flags"]
    rows = crawl + [row("merge", None, start=at(3, 18, 1))]
    later = health.flags(week_of(rows), None, None, at(13, 0, 1))
    assert [(c["job"], c["status"]) for c in later["chain"]] == [
        ("merge", "done"),
        ("enrich", "missing"),
        ("build", "missing"),
        ("publish", "missing"),
    ]
    assert [(x["code"], x["job"]) for x in later["flags"]] == [("chain_missing", "enrich")]


def test_a_failed_build_is_one_flag_and_a_served_build_is_published():
    rows = [row(todo=1, ok=1, errors=0), row("merge", None, start=at(3, 18, 1)),
            row("enrich", None, start=at(4, 18, 1)), row("build", None, start=at(6, 18, 1), status="failed")]  # fmt: skip
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
    codes = sorted({(x["code"], x.get("connector") or x.get("job") or x.get("ip")) for x in f["flags"]})
    assert codes == [("deadline", "generic"), ("deadline", "woocommerce"), ("died", "generic"),
                     ("failed", "fetch-health"), ("jump", "generic"), ("jump", "square_online"),
                     ("jump", "squarespace"), ("refusals", "203.0.113.40")]  # fmt: skip
    ups = {x["connector"]: [u["field"] for u in x["ups"]] for x in f["flags"] if x["code"] == "jump"}
    # 1 store failing last week, 7 or 8 this week: more than chance, as on the production week it is shaped like
    assert ups == {"generic": ["errors", "http_429"], "square_online": ["errors"], "squarespace": ["errors"]}
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
    assert "5 of 5 connectors landed" in answer and "8 things to look at" in answer
    assert "Tuesday’s merge hasn’t run yet" in answer
    assert (
        "generic’s resume crawl died." in text
        and "Over again next week and the egress check fails on it" in text
        and "never reported finishing" in text
    )
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
    """Every crawl schedule in ingestion.tf is one of PASSES' times, each inside its own slot."""
    import re
    from pathlib import Path

    tf = (Path(__file__).parents[1] / "src/terraform/ingestion.tf").read_text()
    crons = set(re.findall(r'job = "crawl-[a-z-]+", cron = "(\d+) (\d+) \* \* 1"', tf))
    times = {(str(t.minute), str(t.hour)) for _, _, t in health.PASSES}
    assert crons == times
    for key, _, t in health.PASSES:
        assert health.slot(row(start=at(t.hour, t.minute)), WEEK) == key
