from coffeedata.classify import is_coffee
from coffeedata.models import CatalogItem, Variant


def item(title, product_type="", tags=(), grams=None, body=""):
    return CatalogItem(
        roaster_id="r",
        product_id="1",
        handle="h",
        url="u",
        title=title,
        platform="shopify",
        product_type=product_type,
        tags=list(tags),
        variants=[Variant(variant_id="v", grams=grams)],
        body_text=body,
    )


def test_obvious_coffees():
    assert is_coffee(item("Ethiopia Guji Natural", grams=340))
    assert is_coffee(item("Hair Bender", product_type="Coffee"))
    assert is_coffee(item("Perennial", grams=340, body="A medium roast with notes of fig and cocoa."))


def test_cup_of_excellence_and_coffee_and_tea_stores():
    assert is_coffee(item("Colombia Cup of Excellence #3", grams=227))
    assert is_coffee(item("Guatemala Huehuetenango", product_type="Coffee & Tea", grams=340))
    assert not is_coffee(item("Earl Grey Tea", product_type="Coffee & Tea"))
    assert not is_coffee(item("Jasmine Green", product_type="Tea"))


def test_confectionery_is_not_coffee():
    assert not is_coffee(item("CHUCULAT Dark Chocolate Covered Maranon Gragee (50g)", product_type="Coffee"))
    assert not is_coffee(item("Espresso Bean Brittle"))


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
    for rid, items in fixture_items.items():
        kept = [i for i in items if is_coffee(i)]
        assert kept, rid
        assert not any("grinder" in i.title.lower() or "mug" in i.title.lower() for i in kept), rid


def test_filter_roast_is_coffee_but_filters_are_not():
    assert is_coffee(item("Ethiopia Guji - Filter Roast", grams=250))
    assert not is_coffee(item("Kalita Wave 185 Filters"))
    assert not is_coffee(item("Paper Filter Cones #2"))


def test_parts_cups_and_cleaners_are_not_coffee():
    for title in (
        "La Marzocco (VITE3X10) Screw",
        "Rancilio (62100000093) Gasket",
        "BFC 0540020 Knob Fixing Pin (Rub.) | Fits Casanova & More",
        "La Marzocco (T.1.054) TUBE, VALVE TO DISCHARGE | Fits Mistral",
        "98mm Premium Cast Steel Burrs (EK43/EK43S)",
        "Espresso Cleaning Brush",
        "Cup and Saucers black",
        "Lungo Cup, COSTA NOVA...Grespresso White",
        "Urnex Cafiza Espresso Cleaner Powder",
        "Panama Relief Donation",
        "Coffee Lovers Gift Basket",
        "787 Coffee's Homemade Piña Colada Sauce",
        "Learn Coffee in Puerto Rico: Hacienda Iluminada Weekend Experience",
    ):
        assert not is_coffee(item(title, grams=340)), title


def test_test_listings_are_not_coffee():
    for title in (
        "E2E Test",
        "[Test] Rafael",
        "TEST BATCH 2 LB",
        "Star of the North - TEST",
        "John Cafe Test Coffee Decaf",
    ):
        assert not is_coffee(item(title, grams=340)), title


def test_coffees_that_look_like_parts_or_tests_stay():
    for title in (
        "BAKRI CUPS Ethiopia Natural",
        "NLU THE EYE TEST",
        "Gold Miners Decaf Test",
        "Dose - Ecuador El Sauce",
        "Sawtooth Blend (Survival Sauce)",
        "Wicked Jack's Coffee - Pirate's Blend (12oz.)",
        "Colombia: Reserva del Patron – Full Bag (154LB)",
        "INDIE COFFEE ROASTERS CIRCLE BLEND WHOLE BEAN 12 OZ - 197644486786",
        "Altura Coffee, Grown at high elevation for a brighter, cleaner cup",
        "Odd and Ends - Donation Coffee",
    ):
        assert is_coffee(item(title, grams=340, body="A medium roast with notes of cocoa.")), title
