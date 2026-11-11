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
        # a pack with an explicit count is what the price buys: the total
        ("12 OZ BAGS x 3 CT / Whole Bean", 1021),
        ("10oz Case Pack (6)", 1701),
        ("Wholesale: 4 / 2 lbs / Ground", 3629),
        ("20/2.00oz Packets / Roast & Ground", 1134),
        ("Retail Case (4 x 12 oz)", 1361),
        ("12oz - 3 Pack / French Press", 1021),
        ("Case of 6 - 10 oz. bags / Whole Bean", 1701),
        ("6 count case (12 oz ground)", 2041),
        ("1.5 oz 12 Pack / GROUND DECAF", 510),
        ("8 / 1 lb Bags / Whole Bean", 3629),
        ("Guatemala 10 oz Tin x 2", 567),
        ("12 oz bag (Bundle of 4)", 1361),
        ("Variety 3 Pack - 12oz Bali - 12oz Peru - 12oz Mexico", 1021),
        ("5lbs (packed in separate 1lb bags)", 2268),
        # a subscription option is one delivery (M5)
        ("12 oz / Monthly", 340),
        ("monthly-10lbs", 4536),
        # "/" or "," right before the number (M4), and a unit glued to digits
        ("Whole Bean/12oz", 340),
        ("Whole Bean,1lb", 454),
        ("5lb15-to-wwa ", 2268),
        # mixed fractions and slug decimals (L1)
        ("1 1/3 lb", 605),
        ("0-5-lb", 227),
        ("2-2-pounds", 998),
        # a word before a weight is not a count unless it reads as one (L2)
        ("Espresso Blend Two 12oz", 340),
        # a coffee in a can is a weight; a "Small Batch" is not a batch size
        ("1 lb. Can / Drip", 454),
        ("Small Batch Colombia 12oz", 340),
        # abstain: a pack or bundle with no count, merchandise, a fraction that reads two ways, drinks
        ("12 oz Bundle", None),
        ("Espresso Blend 12oz + Mug", None),
        ("Kenya AA 4/5 lb", None),
        ("Nitro Cold Brew 12oz Can", None),
        ("Cold Brew Concentrate 32 oz", None),
        ("3 Month Gift Subscription 12oz", None),
        ("12 fl oz", None),
        # a thousands comma is one number (review L2 of #136)
        ("1,000g", 1000),
        ("2,000 g", 2000),
        # a count written as "N bags" is a count (review M1 of #136): the total
        ("Two 12 oz bags", 680),
        ("2 Bags - 12 oz", 680),
        ("12 oz / 2 Bags", 680),
        ("12oz x 2 bags", 680),
        ("4.3 lb / 2 bags", 3901),
        ("(2) 12 oz. bags", 680),
        ("Two 12 oz. Bags / Every Week / Three Months", 680),
        ("Three 12 oz. Bags / Every 4 Weeks / Six Months", 1021),
        ("Default 12oz Whole Beans (Made in Brooklyn since 1972) x 2 Bags", 680),
        # ... one bag is a bag, and a count already read isn't read twice
        ("1 Bag (12 oz)", 340),
        ("One 12 oz. Bag / Every Week", 340),
        ("2x 12oz Whole Bean (for subscription, to select two bags)", 680),
        # ... a count without a size gives no weight; two counts read two ways
        ("Whole Bean / 5 bags", None),
        ("Ground / 2 Bags | 5% off", None),
        ("2 bags of 12oz or 3 bags", None),
        # review H1 of #139: a weight already the total isn't multiplied; a price tier isn't a count; a shape that can
        # be either (each bag's weight, or the total) abstains
        ("2 Bags (24 oz total)", 680),
        ("2 Bags (24oz total) / French Press", 680),
        ("5 lb (packed in 5 bags)", 2268),
        ("24 oz combined, 2 bags", 680),
        ("5lb (4 bags or more) / Ground", None),
        ("12 oz - 2+ bags", None),
        ("24 oz (2 bags)", None),
        ("1 kg - 2 bags", None),
        ("12oz (2 bags)", None),
        ("2 Bags (24 oz)", None),
        # review L1 of #139: one bag is no count, so a bundle or a subscription around it still blocks
        ("1 Bag - 12oz / 3 Months Prepaid", None),
        ("1 Bag 12oz + Mug", None),
        ("One bag of 12oz - Gift Set", None),
        # review L2 of #139: a slug digit pair is a decimal only as stores write them (halves, 2.2 lb)
        ("2-1-lb", None),
        ("1-4-lb ", None),
        ("1-5-lb", 680),
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
        ("Lavazza Super Crema 2.2 lbs pack of 6", 5987),
        ("Original Donut Shop Medium Blend, Portion Pack 48/2 oz", 2722),
        ("Silver Cup Blend DG 2.5 oz, PP 42ct", 2977),
        ("Jamaican Me Crazy Portion Pack 1.5 oz", 43),
        ("DARK ROAST - Cold Brew Blend by the lb.", 454),
        ("Ethiopia Sampler 4 oz", None),
        ("Breakfast Blend K-Cups 12 count", None),
        ("12oz Latte", None),
        ("Inti Honey - 20lb batch", None),
        ("Espresso Double Shot 2oz", None),
        # review M1 of #136: "N bags" anywhere in a title is a count of its bags
        ("Segafredo Intermezzo Whole Beans Coffee 2 Bags X 17.6oz/500g", 998),
        ("Gran Crema Espresso Beans, Two Bags (2.2 lb each)", 1996),
        ("Two 10oz Bags", 567),
        ("Espresso Roast 5lb (Made in Brooklyn NY since 1972) x 2 Bags", 4536),
        ("Espresso Beans (GOLD Label/2 bags)", None),
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
        ("Two 12 oz bags of specialty grade coffee, roasted-to-order", None),
        ("Nationwide Customers- you will receive (2) 12 oz. bags", None),
        ("1 bag of 12oz", 340),
        # a weight outside a bag's range is a lot or a harvest, not a bag: the bag's own line still counts
        ("The first lot was 32 boxes (roughly - 2,000 lbs).\n12 ounce/ 340 grams", 340),
        ("Production is 12 million 70-kilo (154 lbs) bags a year.\nColombia (12oz)", 340),
        # a decaf's Swiss Water Process is not brewing water; "Royal Select" is not a choice of sizes
        ("12oz bag of Colombia Royal Select Swiss Water Process Decaf.", 340),
        ("A 1.5 oz packet of ground Breakfast Blend coffee pre-measured for 8-10 cups of coffee.", 43),
        ("Great as a latte or espresso.\n12 oz bag", 340),
        ("Coffee sold by the pound 16oz", 454),
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
    assert product_grams("Kenya 12oz 6 pack", "12 oz Bag.", [("", 90.0, None)]) == [2041]
    assert product_grams("Kenya 12oz Bundle", "12 oz Bag.", [("", 90.0, None)]) == [None]


def test_options_with_their_own_weights_are_not_overridden_by_the_title():
    opts = [("12 oz", 20.0, 340), ("5 lb", 90.0, 2268), ("Whole Bean", 20.0, None)]
    assert product_grams("Colombia 12oz", "", opts) == [340, 2268, None]


def test_stored_weights_the_crawler_misread_from_the_option_are_corrected_or_dropped():
    opts = [("1/2 lb", 10.0, 907), ("12oz - 3 Pack", 41.85, 340), ("5 lbs (2.27 kg)", 75.0, 2270)]
    # "5 lbs (2.27 kg)": the stored 2270 agrees with 5 lb (2268) to within rounding, so it stays as stored
    assert product_grams("Mexico", "", opts) == [227, 1021, 2270]
    # a stored weight the stricter rules can't read is dropped, not refilled
    assert product_grams("Kenya 12 oz", "", [("Kenya AA 4/5 lb", 20.0, 2268)]) == [None]


def test_stored_weights_from_elsewhere_are_kept_only_when_the_listing_states_them():
    # Shopify's variant grams: kept when the listing states that weight anywhere, else what the listing states,
    # else dropped (often a shipping weight)
    assert product_grams(
        "Ethiopia", "1 pound (16 ounces)", [("Whole", 18.95, 454), ("Decaf", 27.95, 454)]
    ) == [454, 454]
    assert product_grams("5lb Deadman's Reach", "", [("Espresso", 89.95, 2404), ("Drip", 89.95, 2404)]) == [
        2268,
        2268,
    ]
    assert product_grams("Trailblazer", "Light roast", [("Default Title", 19.0, 363)]) == [None]
    assert product_grams("Tasco Coffee", "", [("Single Can", 2.99, 227)]) == [None]
    # the other policies, kept for eval/bag_grams' comparison
    assert product_grams("Trailblazer", "", [("Default Title", 19.0, 340)], unsupported="standard") == [340]
    assert product_grams("Trailblazer", "", [("Default Title", 19.0, 363)], unsupported="standard") == [None]
    assert product_grams("Trailblazer", "", [("Default Title", 19.0, 363)], unsupported="keep") == [363]


def test_a_pack_the_rules_blank_is_not_refilled_from_the_title():
    # H1: the title's weight is one bag's, not the pack's
    assert product_grams("Peru Coffee 12 oz", "", [("12oz Bundle", 45.0, 340)]) == [None]
    assert product_grams("Peru 12 oz", "", [("Single", 15.0, None), ("Bundle of 3", 16.0, None)]) == [
        None,
        None,
    ]
    assert product_grams("Peru 12 oz", "", [("Single", 15.0, None), ("Bundle", 16.0, None)]) == [None, None]


def test_tags_and_handle_fill_only_when_the_title_states_no_weight():
    assert product_grams("Kenya", "", [("Default Title", 20.0, None)], tags=["12oz", "Coffee"]) == [340]
    assert product_grams("Kenya", "", [("", 20.0, None)], tags=["12oz", "5lb"]) == [None]
    assert product_grams("Kenya 1 lb", "", [("", 20.0, None)], tags=["12oz"]) == [454]
    assert product_grams("Peru Cajamarca", "", [("", 20.0, None)], handle="peru-cajamarca-12oz") == [340]
    # a handle copied from another product's page is not read
    assert product_grams("French Roast", "", [("", 20.0, None)], handle="italian-roast-12oz") == [None]
    assert product_grams("French Roast", "", [("", 20.0, None)], handle="copy-of-french-roast-12oz") == [None]


def test_options_that_give_only_a_count_of_bags():
    # review M1 of #136: "3 bags" of no stated size is blank, never one bag's weight; the other options read as usual
    opts = [("1 bag", 15.99, None), ("3 bags", 39.89, None), ("5 bags", 62.99, None)]
    assert product_grams("Organic Colombian Ground -12oz", "", opts, count_fill=False) == [340, None, None]
    opts = [("Drip Grind / 1 Bag", 16.99, 340), ("Drip Grind / 5 bags", 60.0, 340)]
    assert product_grams("Stay Awake", "12 oz bag", opts, count_fill=False) == [340, None]
    # count_fill (the chosen default): the counts take the one bag weight the product states when the prices per
    # bag agree; when they don't, a count of more than one stays blank
    opts = [("1 bag", 15.99, None), ("3 bags", 39.89, None), ("5 bags", 62.99, None)]
    assert product_grams("Organic Colombian Ground -12oz", "", opts) == [340, 1020, 1700]
    assert product_grams("Stay Awake", "12 oz bag", [("1 Bag", 16.99, 340), ("5 bags", 60.0, 340)]) == [
        340,
        1700,
    ]
    opts = [("1 Bag", 10.0, None), ("2 Bags", 60.0, None)]
    assert product_grams("Kenya 12oz", "", opts) == [340, None]
    # review L3 of #139: a count-only option alone is compared with the other options as one bag each, and with
    # nothing to compare it abstains
    assert product_grams("Kenya 12oz", "", [("Whole Bean", 12.0, None), ("5 bags", 15.0, None)]) == [
        340,
        None,
    ]
    assert product_grams("Kenya 12oz", "", [("Whole Bean", 12.0, None), ("5 bags", 58.0, None)]) == [
        340,
        1700,
    ]
    assert product_grams("Kenya 12oz", "", [("5 bags", 58.0, None)]) == [None]
    # a count that is also a pack or a case stays blocked, and so does the rest of the listing
    opts = [("Sumatra / Whole Bean", 15.0, None), ("3 Bag Bonus Pack / Whole Bean", 40.0, None)]
    assert product_grams("All That Dance", "12 oz bags", opts, count_fill=True) == [None, None]
    assert product_grams("Kenya 12oz", "", [("1 Case (12 Bags)", 200.0, 454)]) == [None]


def test_a_count_away_from_the_weight_multiplies_or_abstains():
    title = "Original Espresso Roast 12oz Whole Beans (Made in Brooklyn since 1972) x 2 Bags"
    assert product_grams(title, "", [("Default Title", 39.95, None)]) == [680]
    assert product_grams(title, "", [("Default Title", 39.95, None)], bag_counts="block") == [None]


def test_a_listing_whose_two_bag_option_states_the_total():
    # review H1 of #139 (ironmulecoffee.com's options): the total is the total
    opts = [
        ("1 Bag (12 oz) / Whole Bean", 14.0, 340),
        ("2 Bags (24 oz total) / Whole Bean", 26.0, 680),
        ("5 Pounds - Save 15% / Whole Bean", 50.0, 2268),
    ]
    assert product_grams("Frosted Muzzle - Medium Roast", "", opts) == [340, 680, 2268]


def test_a_handle_with_a_count_before_its_weight():
    # review L2 of #139: "2-1-lb" is not 2.1 lb; in a handle it reads as two 1 lb bags
    assert product_grams("Kenya", "", [("Whole Bean", 30.0, None)], handle="kenya-2-1-lb-bags") == [907]


def test_review_l1_l3_of_136():
    # L1: a slug decimal in the handle is a decimal, as in an option
    assert product_grams(
        "Colombia Huila", "", [("Whole Bean", 10.0, None)], handle="colombia-huila-0-5-lb"
    ) == [227]
    assert product_grams(
        "Colombia Huila", "", [("Whole Bean", 10.0, None)], handle="colombia-huila-2-5-lb"
    ) == [1134]
    # L2: a stored "1,000g" is the weight the option states
    assert product_grams("Ethiopia", None, [("1,000g", 20.0, 1000)]) == [1000]
    # L3: a tag about shipping is not the bag's size
    assert product_grams(
        "Ethiopia", None, [("Whole Bean", 20.0, None)], tags=["Free shipping over 2 lb"]
    ) == [None]
    assert product_grams("Ethiopia", None, [("Whole Bean", 20.0, None)], tags=["Free shipping", "12oz"]) == [
        340
    ]


def test_long_descriptions_stay_linear():
    # L5: the count lookups read a window, not the whole text before each mention
    import time

    body = "12 oz bag of beans. " * 4000
    t = time.perf_counter()
    product_grams("Kenya", body, [("Whole Bean", 20.0, None)])
    assert time.perf_counter() - t < 2.0


def test_no_options_and_no_prices():
    assert product_grams("Kenya 12 oz", "", []) == []
    assert product_grams("Kenya 12 oz", "", [("", None, None)]) == [None]
