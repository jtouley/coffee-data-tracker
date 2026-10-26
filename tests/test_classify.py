from coffeedata.classify import is_coffee
from coffeedata.models import CatalogItem, Variant


def item(title, product_type="", tags=(), grams=None, body=""):
    return CatalogItem(roaster_slug="r", product_id="1", handle="h", url="u", title=title, platform="shopify",
                       product_type=product_type, tags=list(tags),
                       variants=[Variant(variant_id="v", grams=grams)], body_text=body)


def test_obvious_coffees():
    assert is_coffee(item("Ethiopia Guji Natural", grams=340))
    assert is_coffee(item("Hair Bender", product_type="Coffee"))
    assert is_coffee(item("Perennial", grams=340, body="A medium roast with notes of fig and cocoa."))


def test_obvious_non_coffees():
    assert not is_coffee(item("Logo Mug"))
    assert not is_coffee(item("Bring Me Coffee coaster"))
    assert not is_coffee(item("Coffee Subscription", product_type="Coffee"))
    assert not is_coffee(item("Gift Card"))
    assert not is_coffee(item("Single Origin Sampler Set", grams=340))
    assert not is_coffee(item("I Drink Coffee With Cops", body="This shirt is so soft."))
    assert not is_coffee(item("Ode Brew Grinder Gen 2", product_type="Equipment"))


def test_fixture_mix(fixture_items):
    """Recorded storefronts contain both; the classifier must keep most coffees and drop gear."""
    for slug, items in fixture_items.items():
        kept = [i for i in items if is_coffee(i)]
        assert kept, slug
        assert not any("grinder" in i.title.lower() or "mug" in i.title.lower() for i in kept), slug
