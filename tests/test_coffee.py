"""Milestone 5: the coffees list, History, one coffee's page, a shot's page with Correct and Retract, the More
fields, and bag photos served to their owner only."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from coffeedata.lake import Lake

from .test_app import HeaderAuth, W, setup
from .test_bags import jpeg


@pytest.fixture
def world(lake, tmp_path):
    """Two bags of the same coffee and one of another, with shots, and a catalog row for the first coffee."""
    con = lake.connect()
    con.execute("""create table c as select 'onyx.com:1' coffee_id, 'Geometry' title, 'Onyx Coffee Lab' roaster_name,
        'AR' roaster_state, 'Ethiopia' origin_country, 'Guji' origin_region, 'washed' process, 1900 elevation_masl_min,
        2100 elevation_masl_max, ['bergamot', 'peach'] tasting_notes, 5.25 price_per_100g, true in_stock,
        'https://onyxcoffeelab.com/products/geometry' url""")
    lake.write_mart("dim_coffee", con, "select * from c")
    photos_lake = Lake(str(tmp_path / "photos"), tables=("photos",))
    app = create_app(lake, HeaderAuth(), photos_lake=photos_lake)
    c = TestClient(app)
    setup(c)
    for bag in ({"bag_id": "g1-bag", "coffee_id": "onyx.com:1", "coffee_freetext": "Geometry", "roast_date": "2026-10-01"},
                {"bag_id": "g2-bag", "coffee_id": "onyx.com:1", "coffee_freetext": "Geometry", "roast_date": "2026-10-15"},
                {"bag_id": "other", "coffee_freetext": "Mystery Blend"}):  # fmt: skip
        assert c.post("/api/bags", json=bag, headers=W).status_code == 200
    base = {"grinder_id": "g1", "basket_id": "g1-basket", "dose_g": 18, "yield_g": 36, "grinder_setting": 11}
    for sid, bag, day, t, rating in (("a1", "g1-bag", 2, 30, 4), ("a2", "g1-bag", 3, 28, 5), ("b1", "g2-bag", 16, 27, 5),
                                     ("b2", "g2-bag", 17, 33, 3), ("m1", "other", 18, 25, 2)):  # fmt: skip
        body = base | {
            "shot_id": sid,
            "bag_id": bag,
            "brewed_at": f"2026-10-{day:02d}T07:00:00Z",
            "time_s": t,
            "rating": rating,
        }
        assert c.post("/api/shots", json=body, headers=W).status_code == 200
    return c, app, photos_lake


def test_coffees_lists_bags_used_most_recently_first(world):
    c, _, _ = world
    html = c.get("/coffees").text
    order = [m for m in re.findall(r'href="/coffee/([\w-]+)"', html)]
    assert order == ["other", "g2-bag", "g1-bag"]
    assert "best rated 5, Oct 16" in html and 'aria-current="page">Coffees' in html


def test_coffees_empty_state(lake):
    c = TestClient(create_app(lake, HeaderAuth()))
    assert "No bags yet" in c.get("/coffees").text


def test_history_groups_shots_by_bag_newest_first(world):
    c, _, _ = world
    html = c.get("/history").text
    assert re.findall(r'href="/shot/(\w+)"', html) == ["m1", "b2", "b1", "a2", "a1"]
    assert html.count('class="group"') == 3  # one heading per bag, each opening the coffee


def test_coffee_page_has_the_best_shot_chart_bag_shots_and_catalog(world):
    c, _, _ = world
    html = c.get("/coffee/g2-bag").text
    # Best across both bags of this coffee: rating 5, the later one (b1) wins the tie.
    assert "Best so far · rated 5, Oct 16" in html
    assert "All 4 shots" in html and 'class="target"' in html
    assert re.findall(r'href="/shot/(\w+)"', html) == ["b2", "b1"]  # this bag only, newest first
    assert "Bag 2 of 2" in html and 'href="/coffee/g1-bag">‹ earlier' in html
    for fact in (
        "Ethiopia, Guji, washed",
        "1,900–2,100 m",
        "bergamot, peach",
        "$5.25 / 100 g",
        "In stock",
        "onyxcoffeelab.com",
    ):
        assert fact in html
    assert 'rel="noopener noreferrer"' in html


def test_someone_elses_bag_or_shot_is_not_found(world):
    c, _, _ = world
    other = {"x-test-user": "maya", "Accept": "text/html"}
    assert c.get("/coffee/g1-bag", headers=other).status_code == 404
    assert c.get("/shot/a1", headers=other).status_code == 404
    assert c.get("/log?correct=a1", headers=other).status_code == 404


def test_shot_page_and_correcting_a_shot(world):
    c, app, _ = world
    html = c.get("/shot/b2").text
    assert 'href="/log?correct=b2"' in html and "data-retract" in html
    form = json.loads(
        re.search(
            r'<script id="prefill" type="application/json">(.*?)</script>',
            c.get("/log?correct=b2").text,
            re.S,
        ).group(1)
    )
    assert form["correct"] == {"shot_id": "b2", "brewed_at": "2026-10-17T07:00:00+00:00", "time_s": 33.0,
                               "taste": None, "rating": 3, "notes": None}  # fmt: skip
    assert form["bag_id"] == "g2-bag"
    fixed = {
        k: v
        for k, v in form.items()
        if k in ("bag_id", "grinder_id", "basket_id", "dose_g", "yield_g", "grinder_setting")
    }
    body = fixed | {"shot_id": "b2", "brewed_at": form["correct"]["brewed_at"], "time_s": 31, "rating": 4,
                    "notes": "channelled", "water_temp_c": 92.5, "is_milk_drink": True}  # fmt: skip
    assert c.post("/api/shots", json=body, headers=W).json()["written"] is True
    shot = app.state.store.shot("owner", "b2")
    assert (shot.time_s, shot.rating, shot.notes, shot.water_temp_c, shot.is_milk_drink) == (
        31,
        4,
        "channelled",
        92.5,
        True,
    )
    page = c.get("/shot/b2").text
    assert "channelled" in page and "92.5 °C" in page and "with milk" in page


def test_log_shot_has_the_more_fields(world):
    c, _, _ = world
    html = c.get("/log").text
    for el in ('id="water_temp_c"', 'id="notes"', "data-milk", ">More<"):
        assert el in html


def test_photos_are_served_to_their_owner_only(world):
    c, _, photos_lake = world
    r = c.post("/api/photos/p1", content=jpeg(), headers=W | {"Content-Type": "image/jpeg"})
    assert r.json()["status"] == "no_key"
    c.post("/api/bags", json={"bag_id": "g2-bag", "coffee_id": "onyx.com:1", "coffee_freetext": "Geometry",
                              "roast_date": "2026-10-15", "photo_id": "p1"}, headers=W)  # fmt: skip
    got = c.get("/photos/p1")
    assert (
        got.status_code == 200
        and got.headers["content-type"] == "image/jpeg"
        and got.content[:2] == b"\xff\xd8"
    )
    assert got.headers["cache-control"].startswith("private")
    assert c.get("/photos/p1", headers={"x-test-user": "maya"}).status_code == 404
    assert c.get("/photos/nope").status_code == 404
    assert '<img class="cover mt8" src="/photos/p1"' in c.get("/coffee/g2-bag").text
    assert 'loading="lazy"' in c.get("/coffees").text


def test_retract_then_undo_brings_the_shot_back(world):
    c, app, _ = world
    assert c.post("/api/shots/b2/retract", json={}, headers=W).json()["written"] is True
    assert c.get("/shot/b2").status_code == 404
    assert c.post("/api/shots/b2/restore", json={}, headers=W).json()["written"] is True
    assert (
        c.post("/api/shots/b2/restore", json={}, headers=W).json()["written"] is False
    )  # a resend writes nothing
    assert app.state.store.shot("owner", "b2").time_s == 33
    maya = W | {"x-test-user": "maya"}
    assert c.post("/api/shots/b2/restore", json={}, headers=maya).status_code == 404


def test_rows_older_than_six_days_carry_the_date_and_the_coffee_chart_is_plain(world):
    from datetime import UTC, datetime

    from app import views

    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    assert views.day(datetime(2026, 10, 18, 7, 2, tzinfo=UTC), UTC, now) == "Sun 7:02"
    assert views.day(datetime(2026, 10, 11, 7, 2, tzinfo=UTC), UTC, now) == "Oct 11 7:02"
    c, _, _ = world
    html = c.get("/coffee/g2-bag").text
    assert 'class="ord"' not in html and 'class="path"' not in html and 'class="now"' not in html
    assert 'class="taste' not in html.split('class="legend')[1].split("</div>")[0]
