"""Bag capture: photo checks and re-encoding, the Claude read (with a fake client), roaster-then-coffee matching,
the upload route, and the Opus/Haiku comparison's scoring."""

import io
import json
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


LABEL = {"legible": True, "roaster": "Larkspur", "coffee": "Hambela Alaka", "roast_date": "2026-10-18",
         "origin": "Ethiopia", "process": "washed", "weight_g": 340}  # fmt: skip


def test_opus_reads_with_structured_output_and_the_refusal_fallback():
    fake = FakeClient(LABEL)
    r = labels.read(b"jpeg", labels.OPUS, fake)
    assert (
        r.status == "read"
        and r.label["coffee"] == "Hambela Alaka"
        and r.roast_date().isoformat() == "2026-10-18"
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
    assert r["roast_date"] == "2026-10-18"
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
    assert str(bag.roast_date) == "2026-10-18" and json.loads(bag.read_json)["roaster"] == "Larkspur"


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
    case = bag_read.Case(
        "b1", "larkspur.com:1", "Larkspur Roasting Co.", "Hambela Alaka", "2026-10-18", b"jpeg"
    )

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
