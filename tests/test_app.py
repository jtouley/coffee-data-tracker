"""The app over a synthetic local lake: prefill, save, undo, retries, the cache and per-user scoping."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import views
from app.auth import LocalOwner, User
from app.main import create_app
from coffeedata import shots
from coffeedata.lake import Lake
from coffeedata.models import Shot

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
    assert "1:2.02 in 29 s; Wednesday’s shot was 1:2.02 in 27 s." in saved
    today = client.get("/").text
    assert "Pull again" in today and "18.0 → 36.4 g · grind 11" in today
    for page in (saved, today, client.get("/log").text):
        assert not re.search(r"finer|coarser|Try \d", page)
    [s] = [s for s in shots.load_shots(lake) if s.shot_id == "s2"]
    assert (s.user_id, s.source, s.grinder_id, s.bag_id) == ("owner", "app", "g1", "bag1")


def test_default_chart_keeps_one_decimal_on_ratio_ticks():
    assert ">1:2.0</text>" in views.target_svg([])


def _bag(spec: list[tuple[float, float, str | None]]) -> list[Shot]:
    t0 = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
    return [
        Shot(
            shot_id=f"s{i}",
            brewed_at=t0 + timedelta(days=i),
            grinder_setting=11,
            dose_g=18,
            yield_g=y,
            time_s=t,
            rating=4,
            taste=taste,
        )
        for i, (y, t, taste) in enumerate(spec)
    ]


@pytest.mark.parametrize(
    "spec",
    [
        [
            (36, 26, "sour"),
            (36.4, 28, "balanced"),
            (90, 16, "sour"),
            (90, 18, "balanced"),
            (90, 20, "balanced"),
        ],
        [(36, 28, "balanced"), (144, 20, "balanced")],
        [(36 + i * 0.3, 24 + i, "balanced") for i in range(7)] + [(90, 18, "balanced")],
        [(36, 24, "sour"), (36.5, 27, "balanced"), (36, 29, "balanced")],
    ],
)
@pytest.mark.parametrize(("height", "joined"), [(150, True), (196, True), (196, False)])
def test_window_label_never_sits_on_a_mark_or_the_path(spec, height, joined):
    svg = views.target_svg(_bag(spec), height, sequence=joined)
    m = re.search(r'<text class="wl" x="([\d.]+)" y="([\d.]+)" text-anchor="(\w+)"', svg)
    assert m, "the label is shown in every case here"
    box = views.label_box(float(m[1]), float(m[2]), m[3])
    marks = [
        (float(a), float(b)) for a, b in re.findall(r'class="s-[a-z]+" c?x="([\d.]+)" c?y="([\d.]+)"', svg)
    ]
    assert len(marks) == len(spec[-8:])
    for mx, my in marks:  # squares give their corner; allow for it
        assert views._gap(box, (mx, my, mx + 9, my + 9)) >= 2
    path = re.search(r'<polyline class="path" points="([^"]+)"', svg)
    if path:
        pts = [tuple(map(float, p.split(","))) for p in path[1].split()]
        for a, b in zip(pts, pts[1:], strict=False):
            assert views._seg_gap(box, a, b) >= 3


def test_sequence_names_each_shots_ratio():
    assert views.sequence(_bag([(36, 26, "sour"), (90, 16, None)])) == (
        "This bag, in order: 1:2.0 in 26 s sour, 1:5.0 in 16 s."
    )


def test_long_ratio_shots_are_shown_without_time_judgment(client):
    client.post("/api/shots", json=shot(1, yield_g=54, time_s=15), headers=W)
    client.post("/api/shots", json=shot(2, yield_g=72, time_s=12), headers=W)
    for path in ("/", "/saved/s2", "/coffee/bag1"):
        page = client.get(path).text
        assert "Shaded area is the window" in page
        # Both times and both ratios must get distinct positions instead of being clamped together.
        marks = re.findall(r'<circle class="s-balanced" cx="([\d.]+)" cy="([\d.]+)"', page)
        assert len(marks) == 2 and marks[0][0] != marks[1][0] and marks[0][1] != marks[1][1]
    for path in ("/", "/saved/s2"):
        page = client.get(path).text
        assert "1:4.00 in 12 s" in page and "1:3.00 in 15 s" in page
        assert "Ran fast" not in page and "outside the window" not in page


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


def test_saves_go_back_to_where_they_were_opened(lake, client):
    """Gear screens return to the screen that opened them (first run, the Log shot gear picker, Setup), and only
    to those: a crafted `next` goes to Setup."""
    fresh = TestClient(create_app(Lake(str(lake.root.parent / "fresh")), HeaderAuth()))
    assert 'href="/setup/gear/new?kind=grinder&amp;next=/"' in fresh.get("/").text

    picker = client.get("/log/gear?grinder=g1&basket=b1").text
    assert 'href="/setup/gear/new?kind=grinder&amp;next=/log/gear%3Fgrinder%3Dg1%26basket%3Db1"' in picker
    gear = client.get(
        "/setup/gear/new", params={"kind": "basket", "next": "/log/gear?grinder=g1&basket=b1"}
    ).text
    assert 'data-next="/log/gear?grinder=g1&amp;basket=b1"' in gear and "‹ Grinder and basket" in gear
    assert "next=/log/gear%3Fgrinder%3Dg1%26basket%3Db1" in gear  # switching kind keeps where it returns to
    for crafted in ("//evil.example/x", "https://evil.example/", "/history", "javascript:alert(1)"):
        page = client.get("/setup/gear/new", params={"next": crafted}).text
        assert 'data-next="/setup"' in page and "‹ Setup" in page
    assert 'data-next="/setup"' in client.get("/setup/gear/g1").text


def test_a_shot_opened_from_today_goes_back_to_today(client):
    client.post("/api/shots", json=shot(1), headers=W)
    client.post("/api/shots", json=shot(2), headers=W)
    assert 'href="/shot/s1?from=today"' in client.get("/").text
    page = client.get("/shot/s1?from=today").text
    assert '<a class="back" href="/">‹ Today</a>' in page
    assert 'class="on" href="/" aria-current="page">Today' in page
    assert 'class="on" href="/coffees" aria-current="page">Coffees' in client.get("/shot/s1").text


def test_every_page_has_a_place_for_what_the_last_save_did(client):
    for url in ("/", "/log", "/bag/new", "/setup", "/setup/gear/g1", "/coffees", "/offline"):
        assert '<p class="notice" data-notice role="status" hidden></p>' in client.get(url).text, url


@pytest.mark.parametrize(
    "next_, want",
    [
        ("/", ("/", "Today")),
        ("/setup", ("/setup", "Setup")),
        ("/log/gear?grinder=g1&basket=b1&x=1", ("/log/gear?grinder=g1&basket=b1", "Grinder and basket")),
        ("/log/gear?grinder=../../x", ("/log/gear", "Grinder and basket")),
        ("//evil.example", ("/setup", "Setup")),
        ("https://evil.example/", ("/setup", "Setup")),
        ("/\\evil.example", ("/setup", "Setup")),
        ("/history", ("/setup", "Setup")),
        ("", ("/setup", "Setup")),
    ],
)
def test_back_to(next_, want):
    assert views.back_to(next_) == want


def test_times_are_shown_in_the_phones_time_zone(client):
    """The phone writes its zone URL-encoded; read as written, every zone with a "/" fell back to UTC (18:03 shown
    at 14:15 in New York)."""
    client.post("/api/shots", json=shot(1, brewed_at="2026-10-27T18:03:00Z"), headers=W)
    assert "Oct 27, 18:03" in client.get("/shot/s1").text  # no zone yet: UTC
    client.cookies.set("tz", "America%2FNew_York")
    assert "Oct 27, 14:03" in client.get("/shot/s1").text
    assert "14:03" in client.get("/saved/s1").text
    client.cookies.set(
        "tz", "Asia%2FCalcutta"
    )  # Chrome's name for India's zone: an alias, needs the tzdata package
    assert "Oct 27, 23:33" in client.get("/shot/s1").text
    client.cookies.set("tz", "Not%2FA_Zone")
    assert "Oct 27, 18:03" in client.get("/shot/s1").text


def test_a_roast_date_in_the_future_is_refused(client):
    from datetime import date, timedelta

    soon = (date.today() + timedelta(days=3)).isoformat()
    r = client.post(
        "/api/bags", json={"bag_id": "bag9", "coffee_freetext": "x", "roast_date": soon}, headers=W
    )
    assert r.status_code == 422 and "future" in r.text
    tomorrow = (date.today() + timedelta(days=1)).isoformat()  # the phone's date can be a day ahead
    assert (
        client.post(
            "/api/bags", json={"bag_id": "bag9", "coffee_freetext": "x", "roast_date": tomorrow}, headers=W
        ).status_code
        == 200
    )


def test_a_saved_bag_can_be_edited_as_a_new_record(client, lake):
    today, coffee = client.get("/").text, client.get("/coffee/bag1").text
    assert 'href="/bag/bag1/edit?from=today">Edit bag</a>' in today
    assert 'href="/bag/bag1/edit">Edit bag</a>' in coffee
    page = client.get("/bag/bag1/edit?from=today").text
    assert 'value="2026-10-18"' in page and '<a class="back" href="/">‹ Today</a>' in page
    assert 'data-next="/"' in page and "‹ Onyx Geometry" in client.get("/bag/bag1/edit").text
    assert client.get("/bag/nope/edit").status_code == 404
    before = len(files(lake, "bags"))
    record = {
        "bag_id": "bag1",
        "coffee_freetext": "Onyx Geometry",
        "roast_date": "2026-10-15",
        "weight_g": 340,
    }
    assert client.post("/api/bags", json=record, headers=W).status_code == 200
    assert len(files(lake, "bags")) == before + 1  # appended, not rewritten
    assert client.post("/api/bags", json=record, headers=W).status_code == 200  # the outbox resends it
    assert len(files(lake, "bags")) == before + 1
    assert "Roasted Oct 15" in client.get("/").text
    # Another person's bag isn't theirs to open.
    assert client.get("/bag/bag1/edit", headers={"x-test-user": "maya"}).status_code == 404


def test_the_coffee_page_line_reads_well_without_a_roast_date(client):
    client.post(
        "/api/bags", json={"bag_id": "bag2", "coffee_freetext": "x", "finished_on": "2026-10-03"}, headers=W
    )
    assert '<span class="meta">Finished Oct 3</span>' in client.get("/coffee/bag2").text


def test_a_bag_stored_with_a_future_date_can_still_be_saved_unchanged(client, lake):
    """Bags saved before the check may carry a future date: resaving one as it is (a photo's late match does)
    isn't refused; changing it to another future date is."""
    from datetime import date, timedelta

    from app.store import Bag

    store = client.app.state.store
    future = date.today() + timedelta(days=30)
    store._commit(
        store.bags, "old", Bag(bag_id="old", user_id="owner", coffee_freetext="x", roast_date=future), "bags"
    )
    same = {"bag_id": "old", "coffee_freetext": "x", "roast_date": future.isoformat()}
    assert client.post("/api/bags", json=same, headers=W).status_code == 200
    later = same | {"roast_date": (future + timedelta(days=1)).isoformat()}
    assert client.post("/api/bags", json=later, headers=W).status_code == 422
