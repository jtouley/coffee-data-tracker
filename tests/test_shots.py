import pytest

from coffeedata import shots


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    idx = {"onyxcoffeelab:geometry": "Onyx Coffee Lab Geometry",
           "counterculturecoffee:hologram": "Counter Culture Coffee Hologram"}
    monkeypatch.setattr(shots, "_index", lambda: idx)


def test_log_shot_loose_input_matches_catalog(tmp_path):
    path = tmp_path / "shots.jsonl"
    shot, matches = shots.log_shot({"coffee": "onyx geometry", "in": "18g", "out": "38", "time": "29s",
                                    "grind": "15", "rating": 4, "taste": "Sour"}, path)
    assert shot.coffee_id == "onyxcoffeelab:geometry" and shot.dose_g == 18 and shot.taste == "sour"
    assert matches[0]["score"] >= 80
    assert len(shots.load_shots(path)) == 1


def test_freetext_kept_when_no_confident_match(tmp_path):
    shot, _ = shots.log_shot({"coffee": "grandma's tin", "dose": 18, "yield": 36, "time": 28, "grind": 10,
                              "rating": 3}, tmp_path / "s.jsonl")
    assert shot.coffee_id is None and shot.coffee_freetext == "grandma's tin"


def test_invalid_shot_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        shots.log_shot({"coffee": "x", "dose": 80, "yield": 36, "time": 28, "grind": 10, "rating": 3},
                       tmp_path / "s.jsonl")
    assert not (tmp_path / "s.jsonl").exists()


def test_suggest_next(tmp_path):
    p = tmp_path / "s.jsonl"
    assert "No shots" in shots.suggest_next("onyxcoffeelab:geometry", p)
    shots.log_shot({"coffee_id": "onyxcoffeelab:geometry", "dose": 18, "yield": 36, "time": 21, "grind": 15,
                    "rating": 3}, p)
    assert "finer to 14" in shots.suggest_next("onyxcoffeelab:geometry", p)
    shots.log_shot({"coffee_id": "onyxcoffeelab:geometry", "dose": 18, "yield": 36, "time": 35, "grind": 14.5,
                    "rating": 2, "taste": "bitter"}, p)
    assert "coarser to 15" in shots.suggest_next("onyxcoffeelab:geometry", p)
    shots.log_shot({"coffee_id": "onyxcoffeelab:geometry", "dose": 18, "yield": 37, "time": 28, "grind": 14.75,
                    "rating": 5, "taste": "balanced"}, p)
    advice = shots.suggest_next("onyxcoffeelab:geometry", p)
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


def test_parse_issue_body(tmp_path):
    data = shots.parse_issue_body(ISSUE)
    assert data == {"coffee_freetext": "Onyx Geometry", "grinder_setting": "15", "dose_g": "18",
                    "yield_g": "38", "time_s": "29", "rating": "4", "taste": "balanced",
                    "is_milk_drink": True, "notes": "nice"}
    shot, matches = shots.log_shot(data, tmp_path / "s.jsonl")
    reply = shots.issue_reply(shot, matches, tmp_path / "s.jsonl")
    assert "onyxcoffeelab:geometry" in reply and "Last shot" in reply


def test_ambiguous_match_is_not_auto_linked(tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_index", lambda: {"onyxcoffeelab:geometry": "Onyx Coffee Lab Geometry",
                                                  "onyxcoffeelab:monarch": "Onyx Coffee Lab Monarch"})
    shot, matches = shots.log_shot({"coffee": "onyx", "dose": 18, "yield": 36, "time": 28, "grind": 10,
                                    "rating": 3}, tmp_path / "s.jsonl")
    assert shot.coffee_id is None and len(matches) == 2


def test_parse_issue_body_invalid():
    data = shots.parse_issue_body("### Coffee\n\nX\n\n### Dose (g)\n\nlots\n")
    with pytest.raises(ValueError):
        shots.Shot(**shots.normalize(data) | {"shot_id": "x", "brewed_at": "2026-10-26T00:00:00Z"})
