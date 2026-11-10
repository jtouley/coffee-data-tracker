"""coffeedata.bag_weight: bag weights read again from stored text. The cases are shapes seen in the lake's train
split (eval/bag_grams); how often the rules are right is measured there, not here."""

import pytest

from coffeedata.bag_weight import body_grams, product_grams, text_grams


@pytest.mark.parametrize(
    "text, grams",
    [
        # WooCommerce slug options: a hyphen between number and unit (the crawler's parse_grams reads none)
        ("1-lb-custom-ground-medium", 454),
        ("12-ounces  city-light-medium-roast", 340),
        (" 5-lbs", 2268),
        ("2-pounds  ", 907),
        # fractions the crawler misread ("1/2 lb" as 907 g, ".5lb" as 5 lb, "3/4 pound" as 4 lb)
        ("Regular 1/2 lb Whole Bean", 227),
        ("Whole Bean / .5 lb", 227),
        ("3/4 pound (12oz) / Whole Bean", 340),
        ("1 1/2 lb", 680),
        ("Half Pound", 227),
        # an explicit count of bags is the listing's weight
        ("2x1 lb (save 10%) / Whole Bean", 907),
        ("2 x 2.5 LB / Drip Grind", 2268),
        ("Whole bean 2 lb (TWO 16 oz)", 907),
        ("Organic French Roast (5x 1lb bags)", 2268),
        # two units that agree, and pound signs
        ("Net WT. 12oz (340.1g)", 340),
        ("5 lbs (2.27 kg) / Whole Bean", 2268),
        ("5#", 2268),
        ("2.5 # bag / Whole Bean", 1134),
        # a grind or blend number is not a fraction or a count
        ("Espresso / Ground No. 4 / 5 lbs", 2268),
        ("Blend #5", None),
        # abstain: no weight, disagreeing weights, packs, cases, fluid ounces
        ("Whole Bean", None),
        ("12 oz / 5 lb", None),
        ("12 OZ BAGS x 3 CT / Whole Bean", None),
        ("10oz Case Pack (6)", None),
        ("Wholesale: 4 / 2 lbs / Ground", None),
        ("20/2.00oz Packets / Roast & Ground", None),
        ("12 fl oz", None),
        ("1,000g", None),
    ],
)
def test_option_titles(text, grams):
    assert text_grams(text, beans_only=False) == grams


@pytest.mark.parametrize(
    "title, grams",
    [
        ("Bolivian – 1 lb", 454),
        ("Wonderland Blend WB 12OZ, SC", 340),
        ("Dark Sky (French Roast) 8 lb", 3629),
        # a product that isn't one bag of beans: no weight from its title
        ("Lavazza Super Crema 2.2 lbs pack of 6", None),
        ("Ethiopia Sampler 4 oz", None),
        ("Jamaican Me Crazy Portion Pack 1.5 oz", None),
        ("Inti Honey - 20lb batch", None),
        ("Espresso Double Shot 2oz", None),
    ],
)
def test_product_titles(title, grams):
    assert text_grams(title) == grams


@pytest.mark.parametrize(
    "body, grams",
    [
        ("Smooth and sweet.\nCoffee sold by the pound 16oz\nDegree of Roast: 2", 454),
        ("Size: 8oz\nRoasted on: Nov 3", 227),
        ("Swiss Water Process\n16oz (1 lb)\nTasting Notes:", 454),
        ("A bold cup that stands strong.\n12 oz, heat sealed & packaged with love!", 340),
        ("Variety: Geisha\nSize: 8oz", 227),
        ("Available in 12oz only.", 340),
        # brewing amounts are ignored, not a bag
        ("12 oz bag\nBrew 20 g of coffee with 320 g of water", 340),
        ("2-3 Tbs per 12oz of liquid", None),
        # several sizes, lots, shipping, ranges, counts: no weight
        ("Available in 12oz and 5lbs bags", None),
        ("Now offering 24oz instead of 3lbs", None),
        ("With only 20 lbs available to us this year", None),
        ("Limited to 30 lbs!", None),
        ("Limited run of 50 x 4oz bags. Hand numbered", None),
        ("Orders over 2 pounds receive best shipping rate.", None),
        ("each athlete runs with a 20-30 pound ruck sack", None),
        ("12oz. Brewed Coffee", None),
        ("", None),
    ],
)
def test_descriptions(body, grams):
    assert body_grams(body) == grams


def test_product_title_fills_options_that_differ_only_by_grind():
    opts = [("coarse dark", 22.0, None), ("regular medium", 22.0, None), ("espresso dark", 22.0, None)]
    assert product_grams("Bolivian – 1 lb", "", opts) == [454, 454, 454]


def test_a_small_price_gap_is_still_one_size_but_a_large_one_is_not():
    assert product_grams("Uganda by the 1lb", "", [("Whole", 18.99, None), ("Ground", 19.24, None)]) == [
        454,
        454,
    ]
    assert product_grams("Colombia 12oz", "", [("Small", 15.0, None), ("Large", 40.0, None)]) == [None, None]


def test_the_description_is_read_only_when_the_title_states_no_weight():
    assert product_grams("Kenya", "12 oz Bag. Bright.", [("Whole Bean", 20.0, None)]) == [340]
    # the title's weight wins; a title weight the rules reject (a pack) doesn't fall back to the description
    assert product_grams("Kenya 1 lb", "12 oz Bag.", [("", 20.0, None)]) == [454]
    assert product_grams("Kenya 12oz 6 pack", "12 oz Bag.", [("", 90.0, None)]) == [None]


def test_options_with_their_own_weights_are_not_overridden_by_the_title():
    opts = [("12 oz", 20.0, 340), ("5 lb", 90.0, 2268), ("Whole Bean", 20.0, None)]
    assert product_grams("Colombia 12oz", "", opts) == [340, 2268, None]


def test_stored_weights_the_crawler_misread_from_the_option_are_corrected_or_dropped():
    opts = [("1/2 lb", 10.0, 907), ("12oz - 3 Pack", 41.85, 340), ("5 lbs (2.27 kg)", 75.0, 2270)]
    # "5 lbs (2.27 kg)": the stored 2270 agrees with 5 lb (2268) to within rounding, so it stays as stored
    assert product_grams("Mexico", "", opts) == [227, None, 2270]


def test_stored_weights_from_elsewhere_are_kept():
    # Shopify's variant grams, or a generic store's product name: not from the option's title, so kept as stored
    assert product_grams("Ethiopia", "", [("Default Title", 20.0, 400), ("", 20.0, 340)]) == [400, 340]


def test_no_options_and_no_prices():
    assert product_grams("Kenya 12 oz", "", []) == []
    assert product_grams("Kenya 12 oz", "", [("", None, None)]) == [None]
