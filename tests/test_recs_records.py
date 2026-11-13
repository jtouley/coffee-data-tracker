"""The "Your next bag" test's records: coffee ratings and answers to the picks, written by the app the way it writes
shots (one new lake file per record, corrections and undos as new records), gated on the test's consent and limited
per account. Invented coffees and people only."""

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.auth import Sessions
from app.main import create_app
from app.store import Store, response_key

from .test_app import HeaderAuth, W
from .test_invites import accounts, invite, join, owner  # noqa: F401 - fixtures

COFFEES = ("roaster-a.example:1", "roaster-b.example:2", "roaster-c.example:3")
SLATE = "slate-1"


def catalog(lake, coffees=COFFEES) -> None:
    con = lake.connect()
    ids = ", ".join(f"('{c}', 'Coffee {i}', 'Roaster {i}')" for i, c in enumerate(coffees))
    con.execute(f"create table c as select * from (values {ids}) t(coffee_id, title, roaster_name)")
    lake.write_mart("dim_coffee", con, "select * from c")


def slate(lake, rows) -> None:
    """The picks export dbt will write (mart_rec_slate): user_id, slate_id, position, coffee_id."""
    con = lake.connect()
    vals = ", ".join(f"('{u}', '{s}', {p}, '{c}')" for u, s, p, c in rows)
    con.execute(f"create table s as select * from (values {vals}) t(user_id, slate_id, position, coffee_id)")
    lake.write_mart("mart_rec_slate", con, "select * from s")


def files(lake, table: str) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (lake.root / table).glob("*__*.parquet")}


@pytest.fixture
def c(lake):
    catalog(lake)
    return TestClient(create_app(lake, HeaderAuth()))


def rate(c, rating_id="r1", coffee=COFFEES[0], rating=4, user="owner", **kw):
    h = W | {"x-test-user": user, "X-Expected-User": user}
    return c.post(
        "/api/ratings", json={"rating_id": rating_id, "coffee_id": coffee, "rating": rating} | kw, headers=h
    )


def test_a_rating_is_one_new_file_and_corrections_and_undo_are_new_files_never_changes(c, lake):
    r = rate(c, drank_as="espresso")
    assert r.status_code == 200 and r.json() == {"rating_id": "r1", "written": True}
    assert rate(c, drank_as="espresso").json()["written"] is False  # a resend writes nothing
    first = files(lake, "ratings")
    assert len(first) == 1
    assert rate(c, rating=5, drank_as="espresso").json()["written"] is True  # a correction
    assert c.post("/api/ratings/r1/retract", headers=W).json() == {"rating_id": "r1", "written": True}
    assert c.post("/api/ratings/r1/retract", headers=W).json()["written"] is False
    after = files(lake, "ratings")
    assert len(after) == 3 and all(after[n] == b for n, b in first.items())  # nothing stored was rewritten
    rows = sorted((r for f in (lake.root / "ratings").glob("r1__*") for r in pq.read_table(f).to_pylist()),
                  key=lambda r: r["logged_at"])  # fmt: skip
    assert [(r["rating"], bool(r["deleted"])) for r in rows] == [(4, False), (5, False), (5, True)]
    assert {r["user_id"] for r in rows} == {"owner"} and {r["context"] for r in rows} == {"seed"}
    # A late resend of the undone rating doesn't bring it back; changing it is refused.
    assert rate(c, rating=5, drank_as="espresso").json()["written"] is False
    assert rate(c, rating=3).status_code == 422
    # A restart reads the latest of each: undone.
    assert Store(lake, "owner").ratings_of("owner") == []


def test_ratings_are_checked_against_the_catalog_and_each_other(c, lake):
    assert rate(c, coffee="nowhere.example:9").status_code == 422  # not in the catalog
    assert rate(c, rating=6).status_code == 422 and rate(c, rating_id="../x").status_code == 422
    assert rate(c, drank_as="cold brew").status_code == 422 and rate(c, context="other").status_code == 422
    assert rate(c).status_code == 200
    assert rate(c, rating_id="r2").status_code == 422  # the same coffee again: change the first instead
    assert rate(c, coffee=COFFEES[1]).status_code == 422  # a rating can't move to another coffee
    assert rate(c, rating_id="r2", context="later").status_code == 200  # rated again later is its own record
    assert rate(c, rating=2, context="picks").json()["written"] is True
    assert {r.context for r in Store(lake, "owner").ratings_of("owner")} == {"seed", "later"}  # context kept
    # Someone else's rating id is not found, and they can't undo it.
    assert rate(c, user="maya").status_code == 404
    assert (
        c.post(
            "/api/ratings/r1/retract", headers=W | {"x-test-user": "maya", "X-Expected-User": "maya"}
        ).status_code
        == 404
    )


def test_an_unreadable_catalog_is_try_again_not_refused(lake):
    c = TestClient(create_app(lake, HeaderAuth()))  # no catalog export: the read fails
    r = rate(c)
    assert r.status_code == 503 and files(lake, "ratings") == {}


def test_answers_carry_forward_and_one_pick_per_slate(c, lake, monkeypatch):
    from app import store as store_mod

    monkeypatch.setattr(
        store_mod, "SLATE_RECHECK", 0
    )  # a coffee not in the copy held is looked up again at once
    slate(
        lake,
        [("owner", SLATE, i, cid) for i, cid in enumerate(COFFEES)] + [("maya", "slate-2", 0, COFFEES[0])],
    )

    def answer(user="owner", **body):
        h = W | {"x-test-user": user, "X-Expected-User": user}
        return c.post("/api/rec-responses", json={"slate_id": SLATE} | body, headers=h)

    assert answer(coffee_id=COFFEES[0]).status_code == 422  # neither interest nor pick
    assert answer(coffee_id=COFFEES[0], interest="love").status_code == 422
    assert answer(coffee_id="nowhere.example:9", interest="want").status_code == 404  # not one of their picks
    assert (
        answer(user="maya", coffee_id=COFFEES[0], interest="want").status_code == 404
    )  # someone else's slate
    assert answer(coffee_id=COFFEES[0], interest="want").json() == {
        "slate_id": SLATE, "coffee_id": COFFEES[0], "written": True}  # fmt: skip
    assert answer(coffee_id=COFFEES[0], interest="want").json()["written"] is False
    assert answer(coffee_id=COFFEES[0], picked=True).json()["written"] is True  # the interest is kept
    assert answer(coffee_id=COFFEES[1], picked=True).status_code == 200  # picked before answered
    got = {r.coffee_id: (r.interest, r.picked) for r in Store(lake, "owner").responses_of("owner")}
    assert got == {COFFEES[0]: ("want", False), COFFEES[1]: (None, True)}
    names = files(lake, "rec_responses")
    assert len(names) == 4 and all(":" not in n for n in names)
    assert any(n.startswith(response_key("owner", SLATE, COFFEES[0]) + "__") for n in names)


def test_writes_are_limited_per_account_and_resends_are_not_counted(lake, monkeypatch):
    monkeypatch.setattr(app_main, "RECS_PER_HOUR", 2)
    catalog(lake)
    c = TestClient(create_app(lake, HeaderAuth()))
    assert rate(c).status_code == 200
    assert rate(c).status_code == 200  # a resend: not counted
    assert rate(c, rating=5).status_code == 200
    r = rate(c, rating=3)
    assert r.status_code == 429  # kept on the phone and sent again
    assert rate(c, rating_id="m1", user="maya").status_code == 200  # per account


def test_only_members_who_joined_the_test_can_save_ratings(lake, accounts, owner):  # noqa: F811
    catalog(lake)
    app = owner.app
    maya = join(app, invite(owner)["code"])
    uid = accounts.by_name("maya").user_id
    me = W | {"X-Expected-User": uid}
    body = {"rating_id": "r1", "coffee_id": COFFEES[0], "rating": 4}
    assert maya.post("/api/ratings", json=body, headers=me).status_code == 409
    assert files(lake, "ratings") == {}
    assert maya.post("/api/experiment", json={"experiment": "yes"}, headers=me).status_code == 422
    assert maya.post("/api/experiment", json={"experiment": True}, headers=me).json() == {"experiment": True}
    row = {r["user_id"]: r for r in accounts.lake.latest("consents", "user_id")}[uid]
    assert row["experiment"] is True and row["experiment_version"] and row["experiment_at"] is not None
    assert row["terms_version"]  # joining the test keeps the terms acceptance
    assert maya.post("/api/ratings", json=body, headers=me).status_code == 200
    assert maya.post("/api/experiment", json={"experiment": False}, headers=me).json() == {
        "experiment": False
    }
    assert maya.post("/api/ratings", json=body | {"rating": 2}, headers=me).status_code == 409
    assert maya.post("/api/ratings/r1/retract", headers=me).json()["written"] is True  # undo still works
    assert accounts.experiment(uid) is False


@pytest.fixture
def app(lake, accounts):  # noqa: F811
    return create_app(lake, Sessions(accounts))


# --- review fixes: concurrent saves, try-again answers, the limit counting refusals ------------------------------


@pytest.fixture
def slow(lake, monkeypatch):
    """A Store whose publish takes a moment, so two saves overlap the way two phones' requests can."""
    import time

    catalog(lake)
    slate(lake, [("owner", SLATE, i, cid) for i, cid in enumerate(COFFEES)])
    store = Store(lake, "owner")
    publish = lake.publish

    def slowly(*a, **kw):
        time.sleep(0.2)
        return publish(*a, **kw)

    monkeypatch.setattr(lake, "publish", slowly)
    return store


def together(*calls) -> list:
    """Run the calls at the same moment on threads; each result, or the exception it raised."""
    import threading

    out: list = [None] * len(calls)
    start = threading.Barrier(len(calls))

    def go(i, fn):
        start.wait()
        try:
            out[i] = fn()
        except Exception as e:  # noqa: BLE001 - returned for the test to look at
            out[i] = e

    threads = [threading.Thread(target=go, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_two_answers_to_one_coffee_at_once_keep_both_fields(slow):
    # Review B1: without a per-person lock both read "no answer yet" and the later write dropped the other's field.
    together(
        lambda: slow.save_rec_response(
            "owner", {"slate_id": SLATE, "coffee_id": COFFEES[0], "interest": "want"}
        ),
        lambda: slow.save_rec_response("owner", {"slate_id": SLATE, "coffee_id": COFFEES[0], "picked": True}),
    )
    (got,) = slow.responses_of("owner")
    assert (got.interest, got.picked) == ("want", True)
    assert [(r.interest, r.picked) for r in Store(slow.lake, "owner").responses_of("owner")] == [
        ("want", True)
    ]


def test_two_picks_at_once_leave_exactly_one_picked(slow, monkeypatch):
    # The final review's barrier: each pick waits, after its own commit and before its un-pick pass, for the other.
    # Unserialized, both commit, both un-pick passes then see only themselves picked... or none, and none was left
    # picked. Serialized, the first waits alone, times out, and the second un-picks it.
    import contextlib
    import threading

    gate = threading.Barrier(2, timeout=1)
    responses_of = slow._responses_of  # the un-pick pass's read, inside the person's lock

    def at_the_gate(*a, **kw):
        with contextlib.suppress(threading.BrokenBarrierError):
            gate.wait()
        return responses_of(*a, **kw)

    monkeypatch.setattr(slow, "_responses_of", at_the_gate)
    together(*(lambda c=c: slow.save_rec_response("owner", {"slate_id": SLATE, "coffee_id": c, "picked": True})
               for c in COFFEES[:2]))  # fmt: skip
    assert sum(r.picked for r in Store(slow.lake, "owner").responses_of("owner")) == 1


def test_two_new_ratings_of_one_coffee_at_once_store_one(slow):
    from app.store import Invalid

    out = together(*(lambda i=i: slow.save_rating("owner", {"rating_id": f"r{i}", "coffee_id": COFFEES[0],
                                                             "rating": 4}) for i in range(2)))  # fmt: skip
    assert sum(isinstance(x, Invalid) for x in out) == 1
    assert len(Store(slow.lake, "owner").ratings_of("owner")) == 1


def test_a_failed_catalog_read_is_try_again_even_after_an_old_miss(c, lake, monkeypatch):
    # Review S1: a miss cached long ago, then a read that fails, answered "not in the catalog" for good.
    import time

    from app.store import MISS_TTL

    store = c.app.state.store
    store._coffees[COFFEES[0]] = (None, time.monotonic() - MISS_TTL - 1)
    monkeypatch.setattr(lake, "mart_sources", lambda name: [str(lake.root / "nowhere.parquet")])
    assert rate(c).status_code == 503
    assert files(lake, "ratings") == {}


def test_a_new_slate_is_never_refused_by_the_copy_held(c, lake, monkeypatch):
    # Review S2: dbt publishes a new slate while the app holds the old one.
    from app import store as store_mod

    slate(lake, [("owner", SLATE, 0, COFFEES[0])])

    def answer(sid, cid):
        return c.post(
            "/api/rec-responses", json={"slate_id": sid, "coffee_id": cid, "interest": "want"}, headers=W
        )

    assert answer(SLATE, COFFEES[0]).status_code == 200  # the copy is now held
    slate(lake, [("owner", "slate-2", 0, COFFEES[1])])
    assert answer("slate-2", COFFEES[1]).status_code == 503  # read a moment ago: try again, kept on the phone
    monkeypatch.setattr(store_mod, "SLATE_RECHECK", 0)
    assert answer("slate-2", COFFEES[1]).status_code == 200  # read again: found
    assert answer("slate-2", COFFEES[2]).status_code == 404  # not theirs, on a read made just now
    # A failed read keeps the copy held rather than refusing everything.
    monkeypatch.setattr(store_mod, "SLATE_TTL", 0)
    monkeypatch.setattr(lake, "mart_sources", lambda name: ["hf://datasets/nobody/none/x.parquet"])
    store = c.app.state.store

    def broken():
        raise store_mod.Unavailable("down")

    monkeypatch.setattr(store, "_read_slates", broken)
    assert store.slate_pairs("owner") == {("slate-2", COFFEES[1])}


def test_refused_saves_count_toward_the_limit_and_try_again_ones_dont(lake, monkeypatch):
    # Review S3: made-up coffees each cost a catalog read and weren't counted.
    monkeypatch.setattr(app_main, "RECS_PER_HOUR", 2)
    catalog(lake)
    c = TestClient(create_app(lake, HeaderAuth()))
    assert rate(c, rating_id="x1", coffee="made-up.example:1").status_code == 422
    assert rate(c, rating_id="x2", coffee="made-up.example:2").status_code == 422
    assert rate(c).status_code == 429
    d = TestClient(create_app(lake, HeaderAuth()))
    monkeypatch.setattr(lake, "mart_sources", lambda name: [str(lake.root / "nowhere.parquet")])
    for _ in range(3):
        assert rate(d, coffee=COFFEES[1]).status_code == 503  # our side: not counted


def test_a_missing_slate_export_keeps_the_copy_held_and_failed_reads_back_off(lake, monkeypatch):
    # Final review S7 and S8.
    from app import store as store_mod

    slate(lake, [("owner", SLATE, 0, COFFEES[0])])
    store = Store(lake, "owner")
    assert store.slate_pairs("owner") == {(SLATE, COFFEES[0])}
    (lake.root / "marts" / "mart_rec_slate.parquet").unlink()  # mid-publish, or a 404 on the dataset
    monkeypatch.setattr(store_mod, "SLATE_TTL", 0)
    assert store.slate_pairs("owner") == {(SLATE, COFFEES[0])}  # not thrown away as "no picks"
    reads = []
    read = store._read_slates

    def counting():
        reads.append(1)
        return read()

    monkeypatch.setattr(store, "_read_slates", counting)
    for _ in range(5):
        assert store.slate_pairs("owner") == {(SLATE, COFFEES[0])}
    assert reads == []  # backing off after the failed read
    monkeypatch.setattr(store_mod, "SLATE_RECHECK", 0)
    slate(lake, [("owner", "slate-2", 0, COFFEES[1])])
    assert store.slate_pairs("owner") == {("slate-2", COFFEES[1])} and reads == [1]


def member_in_test(lake, accounts):  # noqa: F811
    """The app with accounts, the owner signed in, and maya in the test with a rating and an answer."""
    from .test_invites import PW, client

    catalog(lake)
    app = create_app(lake, Sessions(accounts))
    boss = client(app)
    assert boss.post("/api/signin", json={"agree": True, "setup_code": "SETUP-CODE-1234", "username": "jtouley",
                                          "password": PW}, headers=W).status_code == 200  # fmt: skip
    maya = join(app, invite(boss)["code"])
    uid = accounts.by_name("maya").user_id
    me = W | {"X-Expected-User": uid}
    slate(lake, [(uid, SLATE, i, cid) for i, cid in enumerate(COFFEES)])
    maya.post("/api/experiment", json={"experiment": True}, headers=me)
    rating = {"rating_id": "r1", "coffee_id": COFFEES[0], "rating": 4}
    assert maya.post("/api/ratings", json=rating, headers=me).status_code == 200
    answer = {"slate_id": SLATE, "coffee_id": COFFEES[0], "interest": "want"}
    assert maya.post("/api/rec-responses", json=answer, headers=me).status_code == 200
    return app, boss, maya, uid, me


def responses_on_disk(lake) -> list[tuple]:
    rows = [
        r for f in (lake.root / "rec_responses").glob("*__*.parquet") for r in pq.read_table(f).to_pylist()
    ]
    return sorted((r["coffee_id"], r["interest"], r["picked"]) for r in rows)


def test_only_recs_erasure_holds_in_the_running_app_and_a_rejoin_starts_afresh(lake, accounts):  # noqa: F811
    # Re-check B3: erased while the app runs, with the account on and signed in throughout.
    import time

    from coffeedata import erase
    from coffeedata.lake import Lake

    app, boss, maya, uid, me = member_in_test(lake, accounts)
    photos = Lake(str(lake.root.parent / "photos"), tables=("photos",))
    erase.erase_recs(
        erase.plan("maya", lake, photos, accounts.lake, only_recs=True), lake, photos, accounts.lake
    )
    assert files(lake, "ratings") == {} and files(lake, "rec_responses") == {}
    store = app.state.store
    # The phone still has the Undo, the rating and the answer queued: none brings anything back.
    assert maya.post("/api/ratings/r1/retract", headers=me).status_code == 404
    rating = {"rating_id": "r1", "coffee_id": COFFEES[0], "rating": 4}
    assert maya.post("/api/ratings", json=rating, headers=me).status_code == 409  # erased: join again first
    answer = {"slate_id": SLATE, "coffee_id": COFFEES[0], "picked": True}
    assert maya.post("/api/rec-responses", json=answer, headers=me).status_code == 409
    assert files(lake, "ratings") == {} and files(lake, "rec_responses") == {}
    assert store.ratings_of(uid) == [] and store.responses_of(uid) == []
    assert maya.get("/setup").status_code == 200  # still signed in
    time.sleep(1.1)  # a join after the erasure, not in its second
    assert maya.post("/api/experiment", json={"experiment": True}, headers=me).status_code == 200
    assert maya.post("/api/ratings", json=rating, headers=me).status_code == 422  # that rating id was erased
    assert maya.post("/api/ratings", json=rating | {"rating_id": "r2"}, headers=me).status_code == 200
    assert maya.post("/api/rec-responses", json=answer, headers=me).status_code == 200
    assert responses_on_disk(lake) == [(COFFEES[0], None, True)]  # the erased "want" wasn't merged back
    restarted = create_app(lake, Sessions(accounts)).state.store
    assert [r.rating_id for r in restarted.ratings_of(uid)] == ["r2"]


def test_an_erasure_found_at_start_drops_records_still_on_disk(lake, accounts):  # noqa: F811
    # A file the erasure didn't reach (a staged copy published later) is ignored all the same.
    from datetime import UTC, datetime, timedelta

    app, boss, maya, uid, me = member_in_test(lake, accounts)
    at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    accounts.lake.write("recs_erasures", f"recs_erasures/{uid}__{at:%Y%m%dT%H%M%S}.parquet",
                        [{"user_id": uid, "logged_at": at, "rating_ids": ["r1"]}])  # fmt: skip
    store = create_app(lake, Sessions(accounts)).state.store
    assert store.ratings == {} and store.responses == {}


def test_turning_an_account_off_and_on_keeps_their_test_records(lake, accounts):  # noqa: F811
    # Re-check: the earlier fix dropped them from memory on every turn-off, so the next saves wrote wrong latest
    # records (a stored "want" replaced by null, a duplicate rating let through).
    from .test_invites import client

    app, boss, maya, uid, me = member_in_test(lake, accounts)
    assert boss.post(f"/api/people/{uid}", json={"disabled": True}, headers=W).status_code == 200
    assert boss.post(f"/api/people/{uid}", json={"disabled": False}, headers=W).status_code == 200
    maya = client(app)
    maya.post("/api/signin", json={"username": "maya", "password": "kettle orbit marble"}, headers=W)
    answer = {"slate_id": SLATE, "coffee_id": COFFEES[0], "picked": True}
    assert maya.post("/api/rec-responses", json=answer, headers=me).status_code == 200
    latest = {r.coffee_id: (r.interest, r.picked) for r in app.state.store.responses_of(uid)}
    assert latest == {COFFEES[0]: ("want", True)}
    again = {"rating_id": "r9", "coffee_id": COFFEES[0], "rating": 2}
    assert maya.post("/api/ratings", json=again, headers=me).status_code == 422  # still a duplicate


def test_before_any_picks_export_answers_are_not_found_not_try_again(lake, monkeypatch):
    # Re-check S7: before PR 3 there's no export; every read after the first must still be "no picks".
    from app import store as store_mod

    catalog(lake)
    c = TestClient(create_app(lake, HeaderAuth()))
    monkeypatch.setattr(store_mod, "SLATE_TTL", 0)
    monkeypatch.setattr(store_mod, "SLATE_RECHECK", 0)
    for _ in range(3):
        r = c.post("/api/rec-responses", json={"slate_id": SLATE, "coffee_id": COFFEES[0], "interest": "want"},
                   headers=W)  # fmt: skip
        assert r.status_code == 404
