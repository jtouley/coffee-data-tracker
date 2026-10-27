"""Regressions from the review of milestone 1 (#13)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from coffeedata import shots
from coffeedata.lake import SCHEMA_FILES, Lake

from .test_app import HeaderAuth, W, setup, shot


@pytest.fixture
def c(lake):
    return TestClient(create_app(lake, HeaderAuth()))


def test_publish_adds_a_missing_schema_file_once(tmp_path, monkeypatch):
    """A lake made before equipment/ and bags/ existed gets their schema files with the next publish."""
    import huggingface_hub

    commits: list[list[str]] = []

    class Api:
        def __init__(self, token=None):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            # The old lake has every older table's schema file, but not equipment's or bags'.
            old = set(SCHEMA_FILES.values()) - {SCHEMA_FILES["equipment"], SCHEMA_FILES["bags"]}
            return [type("P", (), {"path": p})() for p in paths if p in old]

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            commits.append(sorted(op.path_in_repo for op in operations))

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    lake = Lake("hf://datasets/me/lake", staging=tmp_path / "staging")
    lake.write("shots", "shots/s1__20261027T070000.parquet", [{"shot_id": "s1"}])
    lake.publish("first")
    assert commits == [
        sorted([SCHEMA_FILES["equipment"], SCHEMA_FILES["bags"], "shots/s1__20261027T070000.parquet"])
    ]
    lake.write("shots", "shots/s2__20261027T070001.parquet", [{"shot_id": "s2"}])
    lake.publish("second")  # schema files aren't looked up again in this process
    assert commits[-1] == ["shots/s2__20261027T070001.parquet"]


def test_a_remote_table_with_no_files_reads_as_staging_only(tmp_path, monkeypatch):
    lake = Lake(str(tmp_path / "lake"))
    lake.remote = True  # as if hf://, with a remote glob that matches nothing
    monkeypatch.setattr(
        lake, "sources", lambda t: [str(lake.root / f"{t}/*.parquet"), str(tmp_path / "nowhere/*.parquet")]
    )
    assert lake.latest("equipment", "equipment_id") == []


def test_grind_is_checked_against_the_grinders_own_dial(c):
    for body in (
        {"equipment_id": "big", "kind": "grinder", "name": "EK43", "scale_min": 0, "scale_max": 160, "scale_step": 1},
        {"equipment_id": "neg", "kind": "grinder", "name": "Comandante", "scale_min": -10, "scale_max": 40, "scale_step": 1},
    ):  # fmt: skip
        assert c.post("/api/equipment", json=body, headers=W).status_code == 200
    base = {
        "brewed_at": "2026-10-27T07:00:00Z",
        "coffee_freetext": "x",
        "dose_g": 18,
        "yield_g": 36,
        "time_s": 28,
        "rating": 3,
    }
    assert (
        c.post(
            "/api/shots", json=base | {"shot_id": "a", "grinder_id": "big", "grinder_setting": 150}, headers=W
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/api/shots", json=base | {"shot_id": "b", "grinder_id": "neg", "grinder_setting": -4}, headers=W
        ).status_code
        == 200
    )
    r = c.post(
        "/api/shots", json=base | {"shot_id": "c", "grinder_id": "neg", "grinder_setting": 41}, headers=W
    )
    assert r.status_code == 422 and "outside Comandante" in r.json()["detail"]


def test_changing_a_bags_text_matches_it_again(c, lake):
    c.post(
        "/api/bags",
        json={"bag_id": "b1", "coffee_id": "onyx.com:1", "coffee_freetext": "Onyx Geometry"},
        headers=W,
    )
    assert (
        c.post("/api/bags", json={"bag_id": "b1", "coffee_freetext": "Onyx Geometry"}, headers=W).json()[
            "coffee_id"
        ]
        == "onyx.com:1"
    )
    r = c.post(
        "/api/bags", json={"bag_id": "b1", "coffee_freetext": "Something else entirely"}, headers=W
    ).json()
    assert r["coffee_id"] is None  # no catalog here, so no match; not the old one


def test_a_late_resend_doesnt_bring_back_a_retracted_shot(c, app_store=None):
    setup(c)
    c.post("/api/bags", json={"bag_id": "bag1", "coffee_freetext": "x"}, headers=W)
    assert c.post("/api/shots", json=shot(1), headers=W).json()["written"] is True
    c.post("/api/shots/s1/retract", headers=W)
    assert c.post("/api/shots", json=shot(1), headers=W).json()["written"] is False
    assert c.get("/saved/s1").status_code == 404


def test_a_coffee_missing_from_the_catalog_is_looked_up_again(lake):
    from app.store import Store

    store = Store(lake, "owner")
    assert store.coffee("onyx.com:1") is None  # no export at all: not cached
    con = lake.connect()
    con.execute(
        "create table c as select 'onyx.com:1' coffee_id, 'Geometry' title, 'Onyx' roaster_name, 'Ethiopia' origin_country, 'washed' process"
    )
    lake.write_mart("dim_coffee", con, "select * from c")
    assert store.coffee("onyx.com:1")["title"] == "Geometry"


def test_grinder_by_name_prefers_a_current_one(lake):
    old = datetime(2026, 1, 1, tzinfo=UTC)
    for eid, retired, at in (("current", False, old), ("retired", True, old + timedelta(days=30))):
        lake.write("equipment", f"equipment/{eid}__x.parquet", [{"equipment_id": eid, "kind": "grinder", "name": "DF64",
                   "retired": retired, "logged_at": at, "scale_min": 0, "scale_max": 90, "scale_step": 1}])  # fmt: skip
    assert shots.grinder_id("df64", lake) == "current"


def test_prefill_skips_a_retired_basket(c):
    setup(c)
    c.post(
        "/api/equipment",
        json={"equipment_id": "b2", "kind": "basket", "name": "VST 20", "basket_dose_g": 20},
        headers=W,
    )
    c.post("/api/bags", json={"bag_id": "bag1", "coffee_freetext": "x"}, headers=W)
    c.post("/api/shots", json=shot(1), headers=W)
    c.post("/api/equipment", json={"equipment_id": "g1-basket", "kind": "basket", "name": "Pullman 18 g", "basket_dose_g": 18,
                                   "retired": True}, headers=W)  # fmt: skip
    assert c.app.state.store.prefill("owner")["basket"].equipment_id == "b2"


def test_setup_counts_only_this_users_shots(c):
    setup(c)
    setup(c, {"x-test-user": "maya"}, grinder="g2")
    c.post("/api/bags", json={"bag_id": "bag1", "coffee_freetext": "x"}, headers=W)
    c.post("/api/shots", json=shot(1), headers=W)
    assert "1 shot" in c.get("/setup").text
    assert "1 shot" not in c.get("/setup", headers={"x-test-user": "maya"}).text


def test_publish_happens_outside_the_store_lock(c, monkeypatch):
    store = c.app.state.store
    seen = []
    monkeypatch.setattr(store.lake, "publish", lambda msg: seen.append(store.lock.locked()) or 0)
    setup(c)
    assert seen and not any(seen)


def test_a_cli_correction_in_the_same_second_wins(lake):
    payload = {
        "shot_id": "s1",
        "coffee": "Geometry",
        "dose": 18,
        "yield": 36,
        "time": 28,
        "grind": 11,
        "rating": 3,
    }
    shots.log_shot(payload, lake)
    shots.log_shot(payload | {"rating": 5}, lake)  # same second: used to raise FileExistsError
    shots.log_shot(payload | {"rating": 4}, lake)
    assert [s.rating for s in shots.load_shots(lake)] == [4]


def test_advice_uses_the_grinders_direction(lake):
    lake.write("equipment", "equipment/rev__x.parquet", [{"equipment_id": "rev", "kind": "grinder", "name": "Rev",
               "finer_is_lower": False, "logged_at": datetime(2026, 1, 1, tzinfo=UTC), "scale_min": 0, "scale_max": 40,
               "scale_step": 1}])  # fmt: skip
    shots.log_shot({"coffee": "Geometry", "dose": 18, "yield": 36, "time": 20, "grind": 20, "rating": 2,
                    "taste": "sour", "grinder_id": "rev"}, lake)  # fmt: skip
    advice = shots.suggest_next("Geometry", lake)
    assert "go finer to 21" in advice  # higher numbers are finer on this grinder
