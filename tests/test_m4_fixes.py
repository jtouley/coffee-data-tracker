"""Regression tests for the #17 review findings on bag capture."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import labels, photos
from app.eval import bag_read
from coffeedata import shots

from .test_app import W
from .test_bags import LABEL, FakeClient, app_with, catalog, jpeg, upload  # noqa: F401 - catalog is a fixture


def test_the_eval_finds_a_photo_in_either_place(catalog, tmp_path, monkeypatch):  # noqa: F811
    # Finding 1: staging and the dataset are read one at a time, so a photo in only one of them is found.
    app, photos_lake = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    r = upload(c).json()
    c.post("/api/bags", json={"bag_id": "b1", "coffee_id": r["match"]["coffee_id"], "photo_id": "p1",
                              "coffee_freetext": "Hambela Alaka"}, headers=W)  # fmt: skip
    real = photos_lake.sources
    monkeypatch.setattr(
        photos_lake, "sources", lambda t: [*real(t), str(tmp_path / "remote" / "photos" / "*.parquet")]
    )
    found = bag_read.cases(catalog, photos_lake)
    assert [x.bag_id for x in found] == ["b1"] and found[0].jpeg[:2] == b"\xff\xd8"


def test_pixel_counts_between_the_limit_and_twice_it_are_refused(monkeypatch):
    # Finding 5: Pillow only raises at twice MAX_IMAGE_PIXELS; between the two it just warns.
    monkeypatch.setattr(photos, "MAX_PIXELS", 6000)
    with pytest.raises(photos.BadPhoto):
        photos.normalize(jpeg(100, 100))  # 10,000 pixels: over 6,000, under 12,000


def test_past_the_days_reads_photos_are_not_kept(catalog, tmp_path):  # noqa: F811
    # Finding 7.
    app, photos_lake = app_with(catalog, tmp_path, FakeClient(LABEL))
    c = TestClient(app)
    for i in range(10):
        upload(c, pid=f"p{i}")
    r = upload(c, pid="p10").json()
    assert r["status"] == "limit" and r["photo_id"] is None
    assert not (photos_lake.root / "photos" / "p10.parquet").exists()


def test_a_roaster_named_inside_other_roasters_names_is_still_searched(lake):
    # Finding 10: "Onyx Coffee", "Coffee Lab" and "Onyx Coffee Lab" all score 100 against "Onyx Coffee Lab".
    shots._catalog_cache.clear()
    con = lake.connect()
    con.execute("""create table c as select * from (values
        ('a:1', 'Onyx Coffee', 'Other'), ('b:1', 'Coffee Lab', 'Other'), ('c:1', 'Coffee', 'Other'),
        ('d:1', 'Lab', 'Other'), ('e:1', 'Onyx Coffee Lab', 'Geometry')
    ) t(coffee_id, roaster_name, title)""")
    lake.write_mart("dim_coffee", con, "select * from c")
    try:
        assert shots.match_bag("Onyx Coffee Lab", "Geometry", lake)[0] == "e:1"
    finally:
        shots._catalog_cache.clear()


def test_failed_reads_dont_count_toward_latency():
    # Finding 11.
    case = bag_read.Case("b1", "x:1", "R", "T", None, b"jpeg")
    slow_fail = iter([labels.Read("failed", latency_s=45.0), labels.Read("read", label=LABEL, latency_s=2.0)])
    scores = bag_read.evaluate([case], [labels.OPUS], 2, lambda *_: next(slow_fail), None)
    assert scores[labels.OPUS].latencies == [2.0]


def test_a_read_is_priced_at_the_model_that_served_it():
    # Finding 12: a server-side fallback can answer from another model.
    fake = FakeClient(LABEL)
    real = fake.beta.messages.create
    fake.beta = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(**vars(real(**kw)) | {"model": labels.HAIKU})
        )
    )
    r = labels.read(b"jpeg", labels.OPUS, fake)
    assert r.model == labels.HAIKU
    assert r.cost_usd == pytest.approx((1500 * 0.10 + 60 * 0.50) / 1e6)
