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


def test_answers_carry_forward_and_one_pick_per_slate(c, lake):
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
