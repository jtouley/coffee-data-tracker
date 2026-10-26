from coffeedata import registry


def test_normalize_domain():
    assert registry.normalize_domain("https://WWW.Example.com/shop?x=1") == "example.com"
    assert registry.normalize_domain("locations.dutchbros.com") == "dutchbros.com"


def test_merge_dedupes_and_skips_social():
    rows = [{"slug": "onyxcoffeelab", "domain": "onyxcoffeelab.com", "sources": "curated"}]
    rows, added = registry.merge(rows, [
        {"domain": "https://www.onyxcoffeelab.com/", "source": "osm"},
        {"domain": "instagram.com", "source": "osm"},
        {"domain": "newroaster.coffee", "name": "New", "source": "osm"},
    ])
    assert added == 1
    by = {r["domain"]: r for r in rows}
    assert by["onyxcoffeelab.com"]["sources"] == "curated+osm"
    assert by["newroaster.coffee"]["slug"] == "newroaster"


def test_registry_file_is_valid():
    rows = registry.load()
    assert len(rows) > 1000
    assert len({r["slug"] for r in rows}) == len(rows) == len({r["domain"] for r in rows})
