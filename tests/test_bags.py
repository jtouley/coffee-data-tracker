"""Bag capture: photo checks and re-encoding, the Claude read (with a fake client), roaster-then-coffee matching,
the upload route, and the Opus/Haiku comparison's scoring."""

import io
import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import labels, photos
from app.eval import bag_read
from app.main import create_app
from coffeedata import shots
from coffeedata.lake import Lake

from .test_app import HeaderAuth, W


def jpeg(w=3000, h=2000, exif: bool = True, orientation: int | None = None, fmt="JPEG") -> bytes:
    im = Image.new("RGB", (w, h), (120, 80, 40))
    ex = Image.Exif()
    if exif:
        ex[0x8825] = {2: (37.0, 46.0, 30.0), 1: "N"}  # GPS IFD: a location
        ex[0x010F] = "PhoneMaker"
    if orientation:
        ex[0x0112] = orientation
    out = io.BytesIO()
    im.save(out, fmt, exif=ex.tobytes() if fmt == "JPEG" else None)
    return out.getvalue()


# --- photos --------------------------------------------------------------------------------------------


def test_photo_is_downsized_and_loses_its_metadata():
    p = photos.normalize(jpeg())
    with Image.open(io.BytesIO(p.jpeg)) as im:
        assert max(im.size) == 1600 and im.format == "JPEG"
        assert not im.getexif() and "exif" not in im.info and "icc_profile" not in im.info
    assert len(p.sha256) == 64 and (p.width, p.height) == (1600, 1067)


def test_photo_is_turned_upright_before_the_orientation_tag_goes():
    p = photos.normalize(jpeg(1200, 800, orientation=6))  # 6: rotate 90°
    assert (p.width, p.height) == (800, 1200)


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"GIF89a" + b"\0" * 64])
def test_bad_uploads_are_refused(data):
    with pytest.raises(photos.BadPhoto):
        photos.normalize(data)


def test_other_image_formats_are_refused():
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, "GIF")
    with pytest.raises(photos.BadPhoto, match="JPEG, PNG or WebP"):
        photos.normalize(out.getvalue())


def test_huge_pixel_counts_are_refused(monkeypatch):
    monkeypatch.setattr(photos, "MAX_PIXELS", 1000)
    with pytest.raises(photos.BadPhoto):
        photos.normalize(jpeg(100, 100))


# --- the read ------------------------------------------------------------------------------------------


class FakeClient:
    """Stands in for anthropic.Anthropic: records requests, returns one canned reply."""

    def __init__(
        self, label: dict | list | None = None, stop: str = "end_turn", error: Exception | None = None
    ):
        """`label` may be a list: one reply per call, in order."""
        self.calls: list[tuple[str, dict]] = []
        self.label, self.stop, self.error = label, stop, error

        def create(kind):
            def go(**kw):
                self.calls.append((kind, kw))
                if self.error:
                    raise self.error
                label = self.label.pop(0) if isinstance(self.label, list) else self.label
                text = json.dumps(label or {})
                return SimpleNamespace(
                    stop_reason=self.stop,
                    stop_details=SimpleNamespace(category="cyber") if self.stop == "refusal" else None,
                    content=[] if self.stop == "refusal" else [SimpleNamespace(type="text", text=text)],
                    usage=SimpleNamespace(input_tokens=1500, output_tokens=60),
                    model=kw["model"],
                )

            return go

        self.messages = SimpleNamespace(create=create("messages"))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create("beta")))


# Ten days ago: a read date over a year old is taken to have the wrong year (labels.latest_past).
ROASTED = (date.today() - timedelta(days=10)).isoformat()
LABEL = {"legible": True, "roaster": "Larkspur", "coffee": "Hambela Alaka", "roast_date": ROASTED,
         "origin": "Ethiopia", "process": "washed", "weight_g": 340}  # fmt: skip


def test_opus_reads_with_structured_output_and_the_refusal_fallback():
    fake = FakeClient(LABEL)
    r = labels.read(b"jpeg", labels.OPUS, fake)
    assert (
        r.status == "read" and r.label["coffee"] == "Hambela Alaka" and r.roast_date().isoformat() == ROASTED
    )
    kind, kw = fake.calls[0]
    assert (
        kind == "beta" and kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    )
    assert kw["output_config"]["format"]["schema"] == labels.SCHEMA and kw["output_config"]["effort"] == "low"
    assert kw["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"
    assert r.cost_usd == pytest.approx((1500 * 4 + 60 * 20) / 1e6)


def test_haiku_has_no_server_side_fallback():
    fake = FakeClient(LABEL)
    assert labels.read(b"jpeg", labels.HAIKU, fake).status == "read"
    kind, kw = fake.calls[0]
    assert kind == "messages" and "fallbacks" not in kw and "betas" not in kw


@pytest.mark.parametrize(
    ("client", "status"),
    [
        (FakeClient(stop="refusal"), "refused"),
        (FakeClient({"legible": False, "roaster": None, "coffee": None}), "illegible"),
        (FakeClient(error=RuntimeError("network")), "failed"),
    ],
)
def test_reads_that_fall_back_to_typing(client, status):
    assert labels.read(b"jpeg", labels.OPUS, client).status == status


def test_no_key_means_type_it(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert labels.read(b"jpeg").status == "no_key"


# --- matching ------------------------------------------------------------------------------------------


@pytest.fixture
def catalog(lake):
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('larkspur.com:1', 'Larkspur Roasting Co.', 'OR', 'Hambela Alaka', 'Ethiopia', 'Guji', 'washed', 6.47, true),
        ('larkspur.com:2', 'Larkspur Roasting Co.', 'OR', 'Hambela Natural', 'Ethiopia', 'Guji', 'natural', 6.10, true),
        ('onyx.com:7', 'Onyx Coffee Lab', 'AR', 'Hambela Alaka Lot 7', 'Ethiopia', null, 'washed', 7.00, false),
        ('onyx.com:1', 'Onyx Coffee Lab', 'AR', 'Geometry', null, null, null, 5.0, true)
    ) t(coffee_id, roaster_name, roaster_state, title, origin_country, origin_region, process, price_per_100g, in_stock)""")
    lake.write_mart("dim_coffee", con, "select *, ['bergamot', 'peach'] as tasting_notes from c")
    yield lake
    shots._catalog_cache.clear()


def test_the_roaster_on_the_bag_decides_between_same_named_coffees(catalog):
    coffee_id, matches = shots.match_bag("Larkspur", "Hambela Alaka", catalog)
    assert coffee_id == "larkspur.com:1"
    assert all(m["coffee_id"].startswith("larkspur") for m in matches)
    assert shots.match_bag("Onyx", "Hambela Alaka", catalog)[0] == "onyx.com:7"


def test_an_unclear_coffee_name_offers_candidates(catalog):
    coffee_id, matches = shots.match_bag("Larkspur", "Hambela", catalog)
    assert coffee_id is None and {m["coffee_id"] for m in matches} == {"larkspur.com:1", "larkspur.com:2"}


def test_no_roaster_falls_back_to_the_whole_catalog(catalog):
    assert shots.match_bag(None, "Geometry", catalog)[0] == "onyx.com:1"
    assert shots.match_bag("", "", catalog) == (None, [])


def test_a_wholesale_bag_matches_its_wholesale_listing_but_twins_are_never_offered(lake):
    # Review #3: only a bag that says wholesale is matched among, and offered, wholesale listings.
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('onyx.com:1', 'Onyx Coffee Lab', 'Ethiopia Guji', false),
        ('onyx.com:2', 'Onyx Coffee Lab', 'Wholesale - 5LB Ethiopia Guji', true),
        ('onyx.com:3', 'Onyx Coffee Lab', 'Kenya Kiambu', false),
        ('onyx.com:4', 'Onyx Coffee Lab', 'Wholesale - 5LB Kenya Kiambu', true)
    ) t(coffee_id, roaster_name, title, is_wholesale)""")
    lake.write_mart("dim_coffee", con, "select * from c")
    try:
        assert shots.match_bag("Onyx", "Wholesale - 5LB Ethiopia Guji", lake)[0] == "onyx.com:2"
        assert shots.match_coffee("Onyx Wholesale 5LB Ethiopia Guji", lake)[0] == "onyx.com:2"
        coffee_id, offered = shots.match_bag("Onyx", "Ethiopia Guji", lake)
        assert coffee_id == "onyx.com:1"  # its wholesale twin is no rival
        assert "onyx.com:2" not in {m["coffee_id"] for m in offered}
        # The roaster only: pick from its retail coffees.
        coffee_id, offered = shots.match_bag("Onyx", None, lake)
        assert coffee_id is None and {m["coffee_id"] for m in offered} == {"onyx.com:1", "onyx.com:3"}
        # A bag that says wholesale but isn't clear: its wholesale listings are among the options.
        coffee_id, offered = shots.match_bag("Onyx", "Wholesale", lake)
        assert coffee_id is None and {"onyx.com:2", "onyx.com:4"} <= {m["coffee_id"] for m in offered}
    finally:
        shots._catalog_cache.clear()


def test_a_coffee_sold_only_as_wholesale_matches_a_bag_that_doesnt_say_so(lake):
    # Re-review #1 and #3: retail is tried first, then the whole catalog. A 5LB cafe bag often says only the
    # coffee's name, and some listings are flagged wholesale only by a tag, so their titles look retail.
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('onyx.com:1', 'Onyx Coffee Lab', 'Ethiopia Guji', false),
        ('onyx.com:2', 'Onyx Coffee Lab', 'Wholesale - 5LB Ethiopia Guji', true),
        ('onyx.com:5', 'Onyx Coffee Lab', 'Wholesale - 5LB Ethiopia Sidama', true),
        ('onyx.com:6', 'Onyx Coffee Lab', 'Colombia Huila', true),
        ('ws.com:1', 'Wholesale Coffee Co', 'Ethiopia Guji', false),
        ('ws.com:2', 'Wholesale Coffee Co', 'Ethiopia Guji 5LB', true)
    ) t(coffee_id, roaster_name, title, is_wholesale)""")
    lake.write_mart("dim_coffee", con, "select * from c")
    try:
        assert shots.match_bag("Onyx", "Ethiopia Sidama", lake)[0] == "onyx.com:5"  # wholesale only
        assert shots.match_coffee("Onyx Ethiopia Sidama", lake)[0] == "onyx.com:5"
        assert shots.match_bag("Onyx", "Colombia Huila", lake)[0] == "onyx.com:6"  # flagged by a tag only
        assert shots.match_bag("Onyx", "Ethiopia Guji", lake)[0] == "onyx.com:1"  # retail still wins the tie
        assert shots.match_coffee("Onyx Ethiopia Guji", lake)[0] == "onyx.com:1"
        # A roaster whose name reads as wholesale doesn't make its bags wholesale.
        assert shots.match_bag("Wholesale Coffee Co", "Ethiopia Guji", lake)[0] == "ws.com:1"
        # Unclear: the wholesale one that outscores the retail candidates is offered, best first.
        coffee_id, offered = shots.match_bag("Onyx", "Sidama", lake, min_score=101)
        assert coffee_id is None and offered[0]["coffee_id"] == "onyx.com:5"
    finally:
        shots._catalog_cache.clear()


def test_a_wholesale_listing_that_outscores_the_retail_pick_makes_the_match_unclear(lake):
    # Re-review at 6cd500b, #1: a tag-only wholesale listing titled exactly as the bag reads (100) against a retail
    # "Ethiopia Sidama Natural" (97). Retail first used to return the 97 confidently and never offer the 100.
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('onyx.com:1', 'Onyx Coffee Lab', 'Ethiopia Sidama Natural', false),
        ('onyx.com:2', 'Onyx Coffee Lab', 'Ethiopia Sidama', true),
        ('onyx.com:3', 'Onyx Coffee Lab', 'Ethiopia Guji', false),
        ('onyx.com:4', 'Onyx Coffee Lab', 'Wholesale - 5LB Ethiopia Guji', true)
    ) t(coffee_id, roaster_name, title, is_wholesale)""")
    lake.write_mart("dim_coffee", con, "select * from c")
    try:
        for coffee_id, offered in (
            shots.match_bag("Onyx", "Ethiopia Sidama", lake),
            shots.match_coffee("Onyx Ethiopia Sidama", lake),
        ):
            assert coffee_id is None
            assert [m["coffee_id"] for m in offered][:2] == ["onyx.com:2", "onyx.com:1"]  # both, best first
        # A twin that only adds words to its retail coffee's title still loses to it.
        assert shots.match_bag("Onyx", "Ethiopia Guji", lake)[0] == "onyx.com:3"
        assert shots.match_coffee("Onyx Ethiopia Guji", lake)[0] == "onyx.com:3"
    finally:
        shots._catalog_cache.clear()


def test_a_failed_catalog_read_is_retried_after_a_minute_not_trusted_for_the_hour(lake, monkeypatch):
    # Re-review #2: a failed read used to stand for the hour, and MCP search then showed wholesale as retail.
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('onyx.com:1', 'Onyx Coffee Lab', 'Ethiopia Guji', false),
        ('onyx.com:2', 'Onyx Coffee Lab', 'Wholesale - 5LB Ethiopia Guji', true)
    ) t(coffee_id, roaster_name, title, is_wholesale)""")
    lake.write_mart("dim_coffee", con, "select * from c")
    connect = Lake.connect

    def down(self, *a, **k):
        raise OSError("HF is down")

    try:
        monkeypatch.setattr(Lake, "connect", down)
        assert shots._catalog(lake) == []
        with pytest.raises(RuntimeError):
            shots.mart_columns(lake)  # MCP search fails for now rather than quietly dropping the filter
        monkeypatch.setattr(Lake, "connect", connect)
        assert shots._catalog(lake) == []  # retried after a minute, not on every call
        now = time.monotonic()
        monkeypatch.setattr(shots.time, "monotonic", lambda: now + shots.FAILED_READ_TTL_S + 1)
        assert {cid for cid, _, _, retail, _ in shots._catalog(lake) if not retail} == {"onyx.com:2"}
        assert "is_wholesale" in shots.mart_columns(lake)
    finally:
        shots._catalog_cache.clear()


# --- the route -----------------------------------------------------------------------------------------


def app_with(lake, tmp_path, client):
    photos_lake = Lake(str(tmp_path / "photos"), tables=("photos",))
    return create_app(lake, HeaderAuth(), photos_lake=photos_lake, labels_client=client), photos_lake


def upload(c, body=None, pid="p1", ctype="image/jpeg"):
    return c.post(f"/api/photos/{pid}", content=body or jpeg(), headers=W | {"Content-Type": ctype})


def test_photo_upload_reads_matches_and_stores_the_photo(catalog, tmp_path):
    fake = FakeClient(LABEL)
    app, photos_lake = app_with(catalog, tmp_path, fake)
    c = TestClient(app)
    r = upload(c).json()
    assert r["status"] == "read" and r["match"]["coffee_id"] == "larkspur.com:1"
    assert r["match"]["notes"] == "bergamot, peach" and r["match"]["price"] == "$6.47 / 100 g"
    assert r["roast_date"] == ROASTED
    # What Claude saw had no metadata either.
    sent = fake.calls[0][1]["messages"][0]["content"][0]["source"]["data"]
    import base64

    with Image.open(io.BytesIO(base64.b64decode(sent))) as im:
        assert not im.getexif() and max(im.size) == 1600
    table = pq.read_table(photos_lake.root / "photos" / "p1.parquet")
    row = table.to_pylist()[0]
    assert (
        row["user_id"] == "owner" and row["image"]["path"] == "p1.jpg" and row["media_type"] == "image/jpeg"
    )
    assert b"huggingface" in table.schema.metadata
    # Using the match makes a bag that remembers the photo and what was read.
    body = {"bag_id": "b1", "coffee_id": r["match"]["coffee_id"], "coffee_freetext": "Hambela Alaka",
            "photo_id": "p1", "read_json": r["raw"], "roast_date": r["roast_date"], "weight_g": 340}  # fmt: skip
    assert c.post("/api/bags", json=body, headers=W).json()["coffee_id"] == "larkspur.com:1"
    bag = app.state.store.bag("owner", "b1")
    assert bag.photo_id == "p1" and json.loads(bag.read_json)["roaster"] == "Larkspur"


@pytest.mark.parametrize("photo_first", [True, False])
def test_a_bag_typed_offline_takes_its_photos_match_whichever_arrives_first(catalog, tmp_path, photo_first):
    # Offline, the person types something the catalog can't match; the photo and the bag sync later.
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    body = {"bag_id": "b1", "coffee_freetext": "the ethiopian one", "photo_id": "p1", "read_pending": True}
    if photo_first:
        upload(c)
    r = c.post("/api/bags", json=body, headers=W).json()
    if not photo_first:
        assert r["coffee_id"] is None
        upload(c)
    bag = app.state.store.bag("owner", "b1")
    assert bag.coffee_id == "larkspur.com:1" and bag.coffee_freetext == "the ethiopian one"
    assert str(bag.roast_date) == ROASTED and json.loads(bag.read_json)["roaster"] == "Larkspur"


def test_a_resent_photo_is_not_read_twice_and_its_id_stays_with_its_owner(catalog, tmp_path):
    fake = FakeClient(LABEL)
    app, _ = app_with(catalog, tmp_path, fake)
    c = TestClient(app)
    first = upload(c).json()
    assert upload(c).json() == first and len(fake.calls) == 1
    other = c.post(
        "/api/photos/p1", content=jpeg(), headers=W | {"Content-Type": "image/jpeg", "x-test-user": "maya"}
    )
    assert other.status_code == 409


def test_a_bag_typed_after_seeing_the_read_keeps_what_was_typed(catalog, tmp_path):
    # #20-1: the photo matched, the person tapped Type and entered something else; the match mustn't come back.
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    assert upload(c).json()["match"]["coffee_id"] == "larkspur.com:1"
    body = {"bag_id": "b9", "coffee_freetext": "a coffee from a market stall", "photo_id": "p1"}
    assert c.post("/api/bags", json=body, headers=W).json()["coffee_id"] is None
    assert app.state.store.bag("owner", "b9").coffee_id is None


def test_a_typed_match_wins_and_another_persons_photo_is_ignored(catalog, tmp_path):
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    upload(c)
    # Typed text that matches keeps its own match.
    r = c.post(
        "/api/bags", json={"bag_id": "b1", "coffee_freetext": "Onyx Geometry", "photo_id": "p1"}, headers=W
    )
    assert r.json()["coffee_id"] == "onyx.com:1"
    # Someone else citing that photo id gets nothing from it.
    maya = W | {"x-test-user": "maya"}
    r = c.post(
        "/api/bags", json={"bag_id": "b2", "coffee_freetext": "something", "photo_id": "p1"}, headers=maya
    )
    assert r.json()["coffee_id"] is None


def test_an_unclear_read_returns_candidates(catalog, tmp_path):
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL | {"coffee": "Hambela"}))
    r = upload(TestClient(app)).json()
    assert r["match"] is None and len(r["candidates"]) == 2


def test_upload_checks(catalog, tmp_path):
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    assert upload(c, ctype="image/gif").status_code == 415
    assert upload(c, b"nope").status_code == 422
    assert upload(c, pid="../x").status_code in (404, 413, 422)  # normalized to /api/x: never a photo
    assert c.post("/api/photos/p9", content=jpeg(), headers={"Content-Type": "image/jpeg"}).status_code == 403
    big = c.post(
        "/api/photos/p2", content=b"x" * (photos.MAX_UPLOAD + 1), headers=W | {"Content-Type": "image/jpeg"}
    )
    assert big.status_code == 413
    # The default limit still applies elsewhere.
    assert (
        c.post(
            "/api/bags", content=b"x" * (70 * 1024), headers=W | {"Content-Type": "application/json"}
        ).status_code
        == 413
    )


def test_reads_are_capped_per_day(catalog, tmp_path):
    fake = FakeClient(LABEL)
    app, _ = app_with(catalog, tmp_path, fake)
    c = TestClient(app)
    statuses = [upload(c, pid=f"p{i}").json()["status"] for i in range(11)]
    assert statuses[:10] == ["read"] * 10 and statuses[10] == "limit" and len(fake.calls) == 10
    # Another person has their own allowance.
    r = c.post(
        "/api/photos/q1", content=jpeg(), headers=W | {"Content-Type": "image/jpeg", "x-test-user": "maya"}
    )
    assert r.json()["status"] == "read"


def test_without_a_key_the_photo_is_kept_and_the_person_types(catalog, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    photos_lake = Lake(str(tmp_path / "photos"), tables=("photos",))
    c = TestClient(create_app(catalog, HeaderAuth(), photos_lake=photos_lake))
    assert "Photo reading is off" in c.get("/bag/new").text
    assert upload(c).json()["status"] == "no_key"
    assert (photos_lake.root / "photos" / "p1.parquet").exists()


def test_camera_is_allowed_only_on_new_bag(catalog, tmp_path):
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    assert "camera=(self)" in c.get("/bag/new").headers["permissions-policy"]
    assert 'capture="environment"' in c.get("/bag/new").text


# --- the comparison ------------------------------------------------------------------------------------


def test_comparison_scores_and_picks_by_the_rule(catalog):
    case = bag_read.Case("b1", "larkspur.com:1", "Larkspur Roasting Co.", "Hambela Alaka", ROASTED, b"jpeg")

    def reader(jpeg_, model):
        if model == labels.OPUS:
            return labels.Read("read", label=LABEL, latency_s=4.0, cost_usd=0.01)
        return labels.Read("read", label=LABEL | {"coffee": "Hambela"}, latency_s=1.0, cost_usd=0.0002)

    scores = bag_read.evaluate([case], [labels.OPUS, labels.HAIKU], 3, reader, catalog)
    opus, haiku = scores[labels.OPUS], scores[labels.HAIKU]
    assert (opus.matched, opus.roaster_ok, opus.coffee_ok, opus.date_ok, opus.agreed) == (3, 3, 3, 3, 1)
    assert haiku.matched == 0 and haiku.coffee_ok == 3  # "Hambela" is in the title, but the match is unclear
    assert bag_read.decide(scores) == labels.OPUS  # Haiku is faster but misses a bag Opus gets
    text = bag_read.report(scores, 1, 3)
    assert "| claude-opus-5-5 | 3/3 |" in text and "re-run at 20" in text


def test_comparison_picks_haiku_when_it_is_as_good_and_faster(catalog):
    case = bag_read.Case("b1", "larkspur.com:1", "Larkspur Roasting Co.", "Hambela Alaka", None, b"jpeg")

    def reader(jpeg_, model):
        return labels.Read("read", label=LABEL, latency_s=4.0 if model == labels.OPUS else 1.0)

    assert (
        bag_read.decide(bag_read.evaluate([case], [labels.OPUS, labels.HAIKU], 2, reader, catalog))
        == labels.HAIKU
    )


def test_two_uploads_of_one_photo_at_once_save_it_once(catalog, tmp_path):
    # A resend can race the first upload; both mustn't write the same file.
    import threading

    p = photos.normalize(jpeg())
    photos_lake = Lake(str(tmp_path / "photos"), tables=("photos",))
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(photos.save(photos_lake, "p1", "owner", p)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, False, False, True]


@pytest.mark.parametrize(
    "read, today, want",
    [
        ("2026-10-15", "2026-10-28", "2026-10-15"),  # within the year: as read
        ("2016-10-15", "2026-10-28", "2026-10-15"),  # the wrong year: this year's
        ("2026-11-02", "2026-10-28", "2025-11-02"),  # in the future: last year's
        ("2026-10-29", "2026-10-28", "2026-10-29"),  # tomorrow: the server's day of grace, not "day 364"
        ("2025-10-28", "2026-10-28", "2025-10-28"),  # a year ago today: as read
        ("2016-02-29", "2026-10-28", "2026-02-28"),  # February 29 in a year without one: the 28th
        ("2024-02-29", "2026-03-01", "2026-02-28"),
    ],
)
def test_a_read_roast_date_with_the_wrong_year_takes_the_latest_past_one(read, today, want):
    got = labels.latest_past(date.fromisoformat(read), date.fromisoformat(today))
    assert (got.isoformat() if got else None) == want


def test_one_farms_lots_show_their_process_and_status_and_the_current_one_first(lake, tmp_path):
    """The owner's bag, Counter Culture's "Finca El Puente - Honey Processed": the catalog row has no country, and
    the card showed "Origin: honey"; the past "Gesha Variety" lot then looked like the right one."""
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('cc:honey', 'Counter Culture Coffee', 'NC', 'Finca El Puente - Honey Processed', null, null, 'honey', true, 'available'),
        ('cc:gesha', 'Counter Culture Coffee', 'NC', 'Finca El Puente - Gesha Variety', 'Honduras', 'Marcala', null, false, 'archived'),
        ('cc:anaerobic', 'Counter Culture Coffee', 'NC', 'Finca El Puente – Anaerobic Washed', 'Honduras', null, 'anaerobic', false, 'archived')
    ) t(coffee_id, roaster_name, roaster_state, title, origin_country, origin_region, process, in_stock, catalog_status)""")  # fmt: skip
    lake.write_mart("dim_coffee", con, "select *, 5.0 as price_per_100g from c")
    app, _ = app_with(
        lake, tmp_path, FakeClient(LABEL | {"roaster": "Counter Culture", "coffee": "Finca El Puente"})
    )
    r = upload(TestClient(app)).json()
    by_id = {m["coffee_id"]: m for m in r["candidates"]}
    assert by_id["cc:honey"]["origin"] == ""  # not "honey": the process isn't a place
    assert by_id["cc:gesha"]["origin"] == "Honduras, Marcala" and by_id["cc:gesha"]["past"] is True
    assert by_id["cc:anaerobic"]["origin"] == "Honduras, anaerobic"
    assert r["candidates"][0]["coffee_id"] == "cc:honey"  # tied on the name: what's sold now comes first
    shots._catalog_cache.clear()


def test_editing_a_bag_to_the_right_lot_matches_it_again(lake):
    """The owner's fix for a bag saved as the wrong lot: Edit bag with the right name looks it up again."""
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('cc:honey', 'Counter Culture Coffee', 'Finca El Puente - Honey Processed', true),
        ('cc:gesha', 'Counter Culture Coffee', 'Finca El Puente - Gesha Variety', false),
        ('cc:sl28', 'Counter Culture Coffee', 'Finca El Puente SL-28 Variety', false),
        ('cc:plain', 'Counter Culture Coffee', 'Finca El Puente', false),
        ('st:1', 'Stumptown', 'Honduras El Puente', false),
        ('nj:1', 'Naomi Joe Coffee Roasters', 'El Puente Natural', false)
    ) t(coffee_id, roaster_name, title, in_stock)""")  # fmt: skip
    lake.write_mart("dim_coffee", con, "select * from c")
    c = TestClient(create_app(lake, HeaderAuth()))
    wrong = {"bag_id": "b1", "coffee_id": "cc:gesha", "coffee_freetext": "Finca El Puente - Gesha Variety"}
    assert c.post("/api/bags", json=wrong, headers=W).status_code == 200
    # What Edit bag sends: the whole record, with the name changed and the old match dropped.
    fixed = wrong | {
        "coffee_freetext": "Finca El Puente - Honey Processed",
        "coffee_id": None,
        "match_score": None,
    }
    assert c.post("/api/bags", json=fixed, headers=W).json()["coffee_id"] == "cc:honey"
    shots._catalog_cache.clear()


def test_catalog_search_reports_unavailable(lake, monkeypatch):
    c = TestClient(create_app(lake, HeaderAuth()))
    monkeypatch.setattr(shots, "_load", lambda lake=None: shots._Catalog(ok=False))
    assert c.get("/api/coffees/search", params={"q": "x"}).json() == {"coffees": []}
    assert c.get("/api/coffees/search", params={"q": "coffee"}).status_code == 503


def test_search_and_explicitly_relink_a_bag_keeps_its_shots(lake):
    from .test_app import setup, shot

    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('cc:honey', 'Counter Culture Coffee', 'Finca El Puente - Honey Processed', true, 'available'),
        ('cc:gesha', 'Counter Culture Coffee', 'Finca El Puente - Gesha Variety', false, 'archived')
    ) t(coffee_id, roaster_name, title, in_stock, catalog_status)""")
    lake.write_mart("dim_coffee", con, "select * from c")
    c = TestClient(create_app(lake, HeaderAuth()))
    setup(c)
    bag = {"bag_id": "bag1", "coffee_id": "cc:gesha", "coffee_freetext": "Finca El Puente - Gesha Variety"}
    assert c.post("/api/bags", json=bag, headers=W).status_code == 200
    assert c.post("/api/shots", json=shot(1), headers=W).status_code == 200
    assert c.get("/api/coffees/search", params={"q": "x"}).json() == {"coffees": []}
    hits = c.get("/api/coffees/search", params={"q": "Counter Culture Finca El Puente"}).json()["coffees"]
    assert {hit["coffee_id"] for hit in hits} == {"cc:honey", "cc:gesha"}
    assert next(hit for hit in hits if hit["coffee_id"] == "cc:gesha")["past"] is True
    edit = c.get("/bag/bag1/edit")
    assert "Search catalog" in edit.text
    fixed = bag | {"coffee_id": "cc:honey", "coffee_freetext": "Finca El Puente - Honey Processed"}
    assert c.post("/api/bags", json=fixed, headers=W).json()["coffee_id"] == "cc:honey"
    detail = c.get("/coffee/bag1").text
    assert "Finca El Puente - Honey Processed" in detail
    assert "This bag" in detail and "/shot/s1" in detail
    shots._catalog_cache.clear()


def test_the_lot_sold_now_survives_the_cut_to_three(lake):
    """Counter Culture's five El Puente lots as in the live catalog: four tie at 97 under the plain (archived)
    "Finca El Puente". Sorted by name, the cut to 3 dropped the one lot still sold."""
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('cc:plain', 'Counter Culture Coffee', 'Finca El Puente', false, 'archived'),
        ('cc:anaerobic', 'Counter Culture Coffee', 'Finca El Puente – Anaerobic Washed', false, 'archived'),
        ('cc:gesha', 'Counter Culture Coffee', 'Finca El Puente - Gesha Variety', false, 'archived'),
        ('cc:honey', 'Counter Culture Coffee', 'Finca El Puente - Honey Processed', true, 'available'),
        ('cc:sl28', 'Counter Culture Coffee', 'Finca El Puente SL-28 Variety', false, 'archived')
    ) t(coffee_id, roaster_name, title, in_stock, catalog_status)""")  # fmt: skip
    lake.write_mart("dim_coffee", con, "select * from c")
    coffee_id, matches = shots.match_bag("Counter Culture", "Finca El Puente", lake)
    assert coffee_id is None  # 100 against 97 isn't clear: the person picks
    assert [m["coffee_id"] for m in matches][:2] == ["cc:plain", "cc:honey"]  # then a past lot, by name
    # The roaster read but not the coffee: its lots, the one sold now first.
    assert shots.match_bag("Counter Culture", None, lake)[1][0]["coffee_id"] == "cc:honey"
    shots._catalog_cache.clear()


def test_a_catalog_without_stock_columns_still_matches(lake):
    """An export from before catalog_status: every lot ranks the same, matching works as before."""
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute(
        "create table c as select * from (values ('x:1', 'Onyx Coffee Lab', 'Geometry')) t(coffee_id, roaster_name, title)"
    )
    lake.write_mart("dim_coffee", con, "select * from c")
    assert shots.match_bag("Onyx", "Geometry", lake)[0] == "x:1"
    shots._catalog_cache.clear()


def test_two_uploads_of_one_photo_at_once_read_it_once(catalog, tmp_path):
    """The outbox can resend a photo still on its way (a reconnect and a page load both flushing): the copies
    wait for each other, so the label is read, and counted against the day's reads, once."""
    import asyncio

    import httpx

    fake = FakeClient([LABEL, LABEL])
    app, _ = app_with(catalog, tmp_path, fake)

    async def both():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:

            def send():
                return c.post("/api/photos/p1", content=jpeg(), headers=W | {"Content-Type": "image/jpeg"})

            return await asyncio.gather(send(), send())

    first, second = asyncio.run(both())
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len(fake.calls) == 1


def test_a_photo_lock_goes_when_its_uploads_end(catalog, tmp_path, monkeypatch):
    """The per-photo locks don't pile up: none is left after a read, a resend, a 409, the day's limit or an error."""
    app, _ = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app, raise_server_exceptions=False)
    assert upload(c).status_code == upload(c).status_code == 200  # a read, then a resend from last_reply
    other = c.post(
        "/api/photos/p1", content=jpeg(), headers=W | {"Content-Type": "image/jpeg", "x-test-user": "maya"}
    )
    assert other.status_code == 409
    for i in range(10):
        upload(c, pid=f"q{i}")
    assert upload(c, pid="over").json()["status"] == "limit"
    assert app.state.photo_locks == {}

    def broken(*a, **k):
        raise RuntimeError("the dataset is down")

    monkeypatch.setattr(photos, "save", broken)
    app, _ = app_with(catalog, tmp_path / "2", FakeClient(LABEL))
    c = TestClient(app, raise_server_exceptions=False)
    assert upload(c).status_code == 500
    assert app.state.photo_locks == {}
