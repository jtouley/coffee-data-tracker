"""The app over a synthetic local lake: prefill, save, undo, retries, the cache and per-user scoping."""

import re

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.auth import LocalOwner, User
from app.main import create_app
from coffeedata import shots
from coffeedata.lake import Lake

from .lakehelp import old_shot

W = {"X-Requested-With": "test"}


class HeaderAuth:
    """Tests only: the user named in a header, to act as two people."""

    owner = User("owner", "owner", "owner")

    def user(self, request: Request) -> User | None:
        name = request.headers.get("x-test-user", "owner")
        return User(name, name, "owner" if name == "owner" else "member")


def setup(c: TestClient, headers: dict | None = None, grinder: str = "g1") -> None:
    h = W | (headers or {})
    for body in (
        {
            "equipment_id": grinder,
            "kind": "grinder",
            "name": "DF64 V2",
            "scale_min": 0,
            "scale_max": 90,
            "scale_step": 0.5,
            "finer_is_lower": True,
        },
        {"equipment_id": f"{grinder}-basket", "kind": "basket", "name": "Pullman 18 g", "basket_dose_g": 18},
    ):
        assert c.post("/api/equipment", json=body, headers=h).status_code == 200


def shot(i: int, **kw) -> dict:
    return {
        "shot_id": f"s{i}",
        "brewed_at": f"2026-10-2{i}T14:0{i}:00.250Z",
        "bag_id": "bag1",
        "grinder_id": "g1",
        "basket_id": "g1-basket",
        "dose_g": 18,
        "yield_g": 36.4,
        "time_s": 27 + i,
        "grinder_setting": 11,
        "rating": 4,
        "taste": "balanced",
    } | kw


def prefill(c: TestClient) -> dict:
    import json

    r = c.get("/log")
    assert r.status_code == 200, r.text
    return json.loads(
        re.search(r'<script id="prefill" type="application/json">(.*?)</script>', r.text, re.S)[1]
    )


@pytest.fixture
def client(lake):
    c = TestClient(create_app(lake, HeaderAuth()))
    setup(c)
    r = c.post(
        "/api/bags",
        json={"bag_id": "bag1", "coffee_freetext": "Onyx Geometry", "roast_date": "2026-10-18"},
        headers=W,
    )
    assert r.status_code == 200
    return c


def files(lake: Lake, table: str) -> list:
    return [p for p in (lake.root / table).glob("*.parquet") if "schema" not in p.name]


def test_first_run_asks_for_equipment(lake):
    c = TestClient(create_app(lake, HeaderAuth()))
    assert "Nothing on the grinder yet" in c.get("/").text
    r = c.get("/log", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    setup(c)
    assert c.get("/log", follow_redirects=False).headers["location"] == "/bag/new"


def test_prefill_from_basket_then_last_shot_of_the_bag(client, lake):
    p = prefill(client)
    # No shot yet: the basket's nominal dose, a 1:2 yield; no time, taste or rating.
    assert (p["dose_g"], p["yield_g"], p["last_time_s"], p["same_as_last"]) == (18.0, 36.0, None, None)
    assert p["grind"] == {"min": 0.0, "max": 90.0, "step": 0.5} and p["bag_id"] == "bag1"

    client.post(
        "/api/shots", json=shot(1, dose_g=18.2, grinder_setting=12.5, taste="sour", rating=3), headers=W
    )
    client.post("/api/shots", json=shot(2, grinder_setting=11, water_temp_c=93), headers=W)
    p = prefill(client)
    assert (p["dose_g"], p["yield_g"], p["grinder_setting"], p["water_temp_c"]) == (18.0, 36.4, 11.0, 93.0)
    assert p["last_time_s"] == 29 and p["same_as_last"] == {"taste": "balanced", "rating": 4}

    # A new bag of the same coffee starts from that coffee's last shot.
    client.post("/api/bags", json={"bag_id": "bag2", "coffee_freetext": "onyx geometry"}, headers=W)
    p = prefill(client)
    assert p["bag_id"] == "bag2" and p["grinder_setting"] == 11.0 and p["last_time_s"] == 29


def test_save_shows_saved_and_today_without_grind_advice(client, lake):
    client.post("/api/shots", json=shot(1, time_s=27), headers=W)
    r = client.post("/api/shots", json=shot(2, time_s=29), headers=W)
    assert r.json() == {"shot_id": "s2", "written": True}
    saved = client.get("/saved/s2?new=1").text
    assert "1:2.02" in saved and "Compared with the last shot" in saved and "data-undo" in saved
    assert "Inside the window, two seconds slower than Wednesday’s shot." in saved
    today = client.get("/").text
    assert "Pull again" in today and "18.0 → 36.4 g · grind 11" in today
    for page in (saved, today, client.get("/log").text):
        assert not re.search(r"finer|coarser|Try \d", page)
    [s] = [s for s in shots.load_shots(lake) if s.shot_id == "s2"]
    assert (s.user_id, s.source, s.grinder_id, s.bag_id) == ("owner", "app", "g1", "bag1")


def test_same_shot_id_twice_writes_once(client, lake):
    """The outbox resends a shot whose acknowledgement was lost: no second record."""
    assert client.post("/api/shots", json=shot(1), headers=W).json()["written"] is True
    assert client.post("/api/shots", json=shot(1), headers=W).json()["written"] is False
    assert len(files(lake, "shots")) == 1
    # The same id with different values is a correction: a new record, and the latest wins.
    assert client.post("/api/shots", json=shot(1, rating=5), headers=W).json()["written"] is True
    assert len(files(lake, "shots")) == 2
    assert [s.rating for s in shots.load_shots(lake)] == [5]


def test_undo_writes_a_retraction(client, lake):
    client.post("/api/shots", json=shot(1), headers=W)
    r = client.post("/api/shots/s1/retract", headers=W)  # within the same second as the save
    assert r.json() == {"shot_id": "s1", "written": True}
    assert client.post("/api/shots/s1/retract", headers=W).json()["written"] is False
    assert len(files(lake, "shots")) == 2  # the shot and its retraction; nothing deleted
    assert shots.load_shots(lake) == []
    assert client.get("/saved/s1").status_code == 404
    assert "No shots of this bag yet" in client.get("/").text


def test_cache_is_seeded_from_the_lake(client, lake, monkeypatch):
    old_shot(lake, "old1", "2026-10-20", coffee_freetext="Onyx Geometry", grinder_setting=14.0)
    client.post("/api/shots", json=shot(1), headers=W)
    client.post("/api/shots", json=shot(2), headers=W)
    client.post("/api/shots/s2/retract", headers=W)
    store = create_app(lake, HeaderAuth()).state.store  # a restart
    assert [s.shot_id for s in store.shots_of("owner")] == ["old1", "s1"]  # old shots are the owner's
    assert store.current_bag("owner").bag_id == "bag1"
    assert [e.name for e in store.equipment_of("owner", "grinder")] == ["DF64 V2"]
    # And it's updated on every write, without reading the lake again.
    monkeypatch.setattr(Lake, "latest", lambda *a: pytest.fail("read the lake again"))
    client.post("/api/shots", json=shot(3), headers=W)
    assert [s.shot_id for s in client.app.state.store.shots_of("owner")] == ["s1", "s3"]


def test_users_see_and_change_only_their_own(client, lake):
    bob = W | {"x-test-user": "bob"}
    client.post("/api/shots", json=shot(1), headers=W)
    assert client.post("/api/shots/s1/retract", headers=bob).status_code == 404
    assert client.post("/api/shots", json=shot(1, rating=1), headers=bob).status_code == 404  # no takeover
    assert client.get("/saved/s1", headers=bob).status_code == 404
    assert (
        client.post("/api/shots", json=shot(9, shot_id="b1"), headers=bob).status_code == 404
    )  # owner's gear
    r = client.post(
        "/api/equipment",
        json={"equipment_id": "g1", "kind": "basket", "name": "x", "basket_dose_g": 18},
        headers=bob,
    )
    assert r.status_code == 404
    assert "Nothing on the grinder yet" in client.get("/", headers=bob).text
    # user_id comes from the sign-in, never from the body.
    setup(client, bob, grinder="bg")
    body = shot(5, shot_id="bob1", user_id="owner", grinder_id="bg", basket_id="bg-basket", bag_id=None)
    assert client.post("/api/shots", json=body, headers=bob).status_code == 200
    assert {s.shot_id: s.user_id for s in shots.load_shots(lake)} == {"s1": "owner", "bob1": "bob"}


def test_writes_need_the_header_and_valid_values(client):
    assert client.post("/api/shots", json=shot(1)).status_code == 403
    r = client.post("/api/shots", json=shot(1, dose_g=80), headers=W)
    assert r.status_code == 422
    r = client.post("/api/equipment", json={"equipment_id": "g2", "kind": "grinder", "name": "x"}, headers=W)
    assert r.status_code == 422 and "scale_min" in r.text
    assert client.post("/api/shots", json=shot(1, shot_id="../x"), headers=W).status_code == 422


def test_local_sign_in_stub_refuses_to_run_on_a_space(monkeypatch):
    monkeypatch.setenv("SPACE_ID", "someone/registry-coffee")
    with pytest.raises(RuntimeError):
        LocalOwner()
