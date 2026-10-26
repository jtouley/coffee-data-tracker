import pytest

from coffeedata.models import Shot, parse_grams


@pytest.mark.parametrize("text,grams", [
    ("12 oz", 340), ("12oz Bag", 340), ("2 lb", 907), ("5 lbs", 2268), ("2#", 907), ("250g", 250),
    ("1 kg", 1000), ("Whole Bean / 340 grams", 340), ("Default Title", None), ("2 oz sample", 57),
])
def test_parse_grams(text, grams):
    assert parse_grams(text) == grams


def test_shot_validation_ranges():
    base = dict(shot_id="x", brewed_at="2026-10-26T08:00:00Z", grinder_setting=15, dose_g=18, yield_g=36,
                time_s=28, rating=4)
    assert Shot(**base).taste is None
    with pytest.raises(ValueError):
        Shot(**base | {"dose_g": 50})
    with pytest.raises(ValueError):
        Shot(**base | {"rating": 6})
    with pytest.raises(ValueError):
        Shot(**base | {"bogus": 1})
