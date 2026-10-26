import json
from datetime import UTC, datetime

from coffeedata import store


def test_save_is_idempotent_and_tracks_changes(tmp_path, fixture_items):
    items = fixture_items["onyxcoffeelab"]
    t0 = datetime(2026, 10, 26, 12, tzinfo=UTC)
    first = store.save("onyx", items, tmp_path, t0)
    assert first and all(e["event"] == "new" for e in first)
    assert store.save("onyx", items, tmp_path, t0) == []  # immediate re-run appends nothing

    changed = [i.model_copy(deep=True) for i in items[1:]]
    changed[0].variants[0].price_usd = (changed[0].variants[0].price_usd or 0) + 1
    events = store.save("onyx", changed, tmp_path, t0.replace(day=27))
    kinds = {e["event"] for e in events}
    assert kinds == {"price", "removed"}
    snap = json.loads((tmp_path / "catalog" / "onyx.json").read_text())
    assert all(p["first_seen"] == "2026-10-26" for p in snap)
    assert "body_text" not in snap[0]
    lines = [ln for f in sorted((tmp_path / "observations").rglob("*.jsonl")) for ln in f.read_text().splitlines()]
    assert len(lines) == len(first) + len(events)


def test_duplicate_handles_get_product_id_suffix(tmp_path, fixture_items):
    a, b = (i.model_copy(deep=True) for i in fixture_items["anaheimcoffee"][:2])
    b.handle = a.handle
    store.save("x", [a, b], tmp_path)
    snap = json.loads((tmp_path / "catalog" / "x.json").read_text())
    assert len({p["handle"] for p in snap}) == 2
