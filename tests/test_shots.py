import pytest

from coffeedata import shots


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    idx = {"onyxcoffeelab.com:1": "Onyx Coffee Lab Geometry", "counterculturecoffee.com:2": "Counter Culture Coffee Hologram"}
    monkeypatch.setattr(shots, "_index", lambda lake=None: idx)


def test_log_shot_loose_input_matches_catalog(lake):
    shot, matches = shots.log_shot({"coffee": "onyx geometry", "in": "18g", "out": "38", "time": "29s",
                                    "grind": "15", "rating": 4, "taste": "Sour"}, lake)
    assert shot.coffee_id == "onyxcoffeelab.com:1" and shot.dose_g == 18 and shot.taste == "sour"
    assert matches[0]["score"] >= 80
    assert len(shots.load_shots(lake)) == 1


def test_ambiguous_match_is_not_auto_linked(lake, monkeypatch):
    monkeypatch.setattr(shots, "_index", lambda lake=None: {"o:1": "Onyx Coffee Lab Geometry", "o:2": "Onyx Coffee Lab Monarch"})
    shot, matches = shots.log_shot({"coffee": "onyx", "dose": 18, "yield": 36, "time": 28, "grind": 10, "rating": 3}, lake)
    assert shot.coffee_id is None and len(matches) == 2


def test_invalid_shot_is_rejected(lake):
    with pytest.raises(ValueError):
        shots.log_shot({"coffee": "x", "dose": 80, "yield": 36, "time": 28, "grind": 10, "rating": 3}, lake)
    assert shots.load_shots(lake) == []


def test_corrections_and_retractions_are_new_records(lake):
    shot, _ = shots.log_shot({"coffee_id": "o:1", "dose": 18, "yield": 36, "time": 28, "grind": 10, "rating": 3}, lake)
    import time
    time.sleep(1.1)  # records are ordered by logged_at (seconds)
    shots.log_shot({"shot_id": shot.shot_id, "brewed_at": shot.brewed_at.isoformat(), "coffee_id": "o:1",
                    "dose": 18, "yield": 36, "time": 28, "grind": 10, "rating": 5}, lake)
    [current] = shots.load_shots(lake)
    assert current.rating == 5
    time.sleep(1.1)
    shots.log_shot({"shot_id": shot.shot_id, "brewed_at": shot.brewed_at.isoformat(), "coffee_id": "o:1",
                    "dose": 18, "yield": 36, "time": 28, "grind": 10, "rating": 5, "deleted": True}, lake)
    assert shots.load_shots(lake) == []
    assert len(list((lake.root / "shots").glob("*__*.parquet"))) == 3  # nothing was overwritten


def test_suggest_next(lake):
    assert "No shots" in shots.suggest_next("o:1", lake)
    shots.log_shot({"coffee_id": "o:1", "dose": 18, "yield": 36, "time": 21, "grind": 15, "rating": 3,
                    "brewed_at": "2026-10-26T08:00:00Z"}, lake)
    assert "finer to 14" in shots.suggest_next("o:1", lake)
    shots.log_shot({"coffee_id": "o:1", "dose": 18, "yield": 36, "time": 35, "grind": 14.5, "rating": 2,
                    "taste": "bitter", "brewed_at": "2026-10-26T08:05:00Z"}, lake)
    assert "coarser to 15" in shots.suggest_next("o:1", lake)
    shots.log_shot({"coffee_id": "o:1", "dose": 18, "yield": 37, "time": 28, "grind": 14.75, "rating": 5,
                    "taste": "balanced", "brewed_at": "2026-10-26T08:10:00Z"}, lake)
    advice = shots.suggest_next("o:1", lake)
    assert "in the window" in advice and "best-rated" in advice


ISSUE = """### Coffee

Onyx Geometry

### Grind setting

15

### Dose (g)

18

### Yield (g)

38

### Time (s)

29

### Rating

4 - good

### Taste

balanced

### Water temp (°C)

_No response_

### Milk drink

- [x] This was a milk drink

### Notes

nice
"""


def test_parse_issue_body(lake):
    data = shots.parse_issue_body(ISSUE)
    assert data == {"coffee_freetext": "Onyx Geometry", "grinder_setting": "15", "dose_g": "18",
                    "yield_g": "38", "time_s": "29", "rating": "4", "taste": "balanced",
                    "is_milk_drink": True, "notes": "nice"}
    shot, matches = shots.log_shot(data, lake)
    reply = shots.issue_reply(shot, matches, lake)
    assert "onyxcoffeelab.com:1" in reply and "Last shot" in reply


def test_parse_issue_body_invalid():
    data = shots.parse_issue_body("### Coffee\n\nX\n\n### Dose (g)\n\nlots\n")
    with pytest.raises(ValueError):
        shots.Shot(**shots.normalize(data) | {"shot_id": "x", "brewed_at": "2026-10-26T00:00:00Z"})


def test_index_reads_dim_coffee_export(lake, monkeypatch):
    monkeypatch.undo()
    import duckdb
    lake.write_mart("dim_coffee", duckdb.connect(), "select 'a.com:1' as coffee_id, 'A Roasters' as roaster_name, 'Geometry' as title")
    shots._index_cache.clear()
    assert shots._index(lake) == {"a.com:1": "A Roasters Geometry"}


@pytest.mark.parametrize("bad", ["../../escaped", "a/b", "..", "x" * 65])
def test_shot_id_cannot_escape_the_lake(lake, bad):
    with pytest.raises(ValueError):
        shots.log_shot({"shot_id": bad, "coffee_id": "o:1", "dose": 18, "yield": 36, "time": 28, "grind": 10,
                        "rating": 3}, lake)
    assert not any(lake.root.parent.glob("escaped*"))


def test_lake_refuses_paths_outside_root(lake):
    with pytest.raises(ValueError):
        lake.write("shots", "shots/../../outside.parquet", [{"shot_id": "x"}])
