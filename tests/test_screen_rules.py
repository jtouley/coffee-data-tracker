"""The roaster screen's features and tier (src/coffeedata/screen_rules.py; docs/design/roaster-screen.md, section 4).

The named cases the design asks to keep (section 5.6). Every store, page and product here is invented for the test:
nothing was fetched, and none of it is a real site's content. The accuracy of the rules is measured in
eval/roaster_screen, not here; these pin how each rule decides.
"""

from __future__ import annotations

import pytest

from coffeedata import screen_rules as sr

KNOWN = sr.KnownRoasters(
    ["Stumptown Coffee Roasters", "Counter Culture Coffee", "Onyx Coffee Lab", "Single Origin Coffee Co"]
)
ORIGINS = ["Ethiopia", "Colombia", "Guatemala", "Kenya", "Peru", "Honduras", "Brazil", "Rwanda"]


def coffee(
    i: int, vendor: str = "", title: str | None = None, sizes=((340, "12 oz"), (907, "2 lb")), **kw
) -> dict:
    origin = ORIGINS[i % len(ORIGINS)]
    return {
        "product_id": str(i), "handle": f"coffee-{i}", "title": title or f"{origin} Single Origin Coffee",
        "vendor": vendor, "product_type": "Coffee", "tags": ["whole bean"],
        "variants": [{"title": t, "grams": g, "price": 18.0 + n, "currency": "USD", "available": True}
                     for n, (g, t) in enumerate(sizes)],
        "body_hash": f"h{i}", "body_text": f"Origin: {origin}. Roast: Light. Tasting notes: peach, jasmine.",
    } | kw  # fmt: skip


def mug(i: int, vendor: str = "") -> dict:
    return {"product_id": f"m{i}", "handle": f"mug-{i}", "title": f"Diner Mug {i}", "vendor": vendor,
            "product_type": "Merch", "tags": ["mug"], "body_hash": f"m{i}", "body_text": "A sturdy ceramic mug.",
            "variants": [{"title": "Default", "grams": None, "price": 14.0, "currency": "USD", "available": True}]}  # fmt: skip


def score(
    name: str, domain: str, products: list[dict], home_text: str, **screen
) -> tuple[str, list[str], dict]:
    rows = [sr.product_signals(p, name, domain, KNOWN) for p in products]
    row = {
        "status": "ok",
        "refusal": None,
        "platform": "shopify",
        "page_full": False,
        "page_timeout": False,
    } | screen
    f = sr.features(row, rows, sr.home_signals(home_text, name, KNOWN))
    return (*sr.tier(f), f)


# --- the named cases -------------------------------------------------------------------------------------------------


def test_germack_a_roaster_under_its_own_name_is_suggested():
    """The case that started this (notes/missing-roasters/germackcoffee.md): a roaster Overture doesn't tag as one.
    The page is invented: its own name on every bag, several origins, and roastery wording on the home page."""
    vendor = "Germack Coffee Roasting Co."
    products = [coffee(i, vendor) for i in range(8)] + [mug(1, vendor)]
    tier, reasons, f = score("Germack Coffee Roasting Co", "germackcoffee.com", products,
                             "Detroit's oldest roaster. We roast in small batches every week.")  # fmt: skip
    assert tier == "suggested"
    assert (f["named"], f["products"], f["own_vendor_share"], f["origins"]) == (8, 9, 1.0, 8)
    assert reasons[:4] == ["named 8/9", "own_vendor 8/8", "roast_words", "origins 8"]
    assert not any(r.startswith(sr.RESELLER_CODES) for r in reasons)


def test_rival_bros_a_roaster_tagged_only_as_a_coffee_shop_is_suggested_by_its_own_brand():
    # no roastery wording on the home page: its own name as the vendor of its coffees is the evidence
    products = [coffee(i, "Rival Bros Coffee") for i in range(6)]
    tier, reasons, f = score("Rival Bros Coffee Bar", "rivalbros.com", products, "Open daily 7 to 5.")
    assert (tier, f["own_vendor_share"], f["roast_phrase"]) == ("suggested", 1.0, None)


def test_a_cafe_that_proudly_serves_a_known_roaster_goes_to_review_never_suggested():
    products = [coffee(i, "Stumptown Coffee Roasters") for i in range(6)]
    tier, reasons, f = score("Corner Cafe", "cornercafe.example", products,
                             "Breakfast all day. Proudly serving Stumptown Coffee Roasters since 2012.")  # fmt: skip
    assert tier == "review"
    assert reasons[0] == "other_roaster 6/6" and any(r.startswith("serves:") for r in reasons)
    assert (f["own_vendor"], f["serves_phrase"]) == (0, "Proudly serving Stumptown Coffee Roasters")


def test_the_counter_selling_counter_culture_is_not_its_own_brand():
    """Own brand is exact, never a substring: "Counter Culture Coffee" is not the brand of a café called The Counter."""
    assert not sr.is_own("Counter Culture Coffee", "The Counter", "thecounter.example")
    assert sr.is_own("The Counter", "The Counter Cafe", "thecounter.example")
    products = [coffee(i, "Counter Culture Coffee") for i in range(6)]
    tier, reasons, f = score(
        "The Counter", "thecounter.example", products, "Coffee, pastries and a long counter."
    )
    assert (tier, f["own_vendor"], f["other_roaster"]) == ("review", 0, 6)


def test_a_wholesale_only_store_goes_to_review():
    products = [coffee(i, "Made Up Roasters", title=f"Wholesale - 5LB {ORIGINS[i]} Coffee", sizes=((2268, "5 lb"),))
                for i in range(6)]  # fmt: skip
    tier, reasons, f = score(
        "Made Up Roasters", "madeup.example", products, "We roast for cafes and offices."
    )
    assert (tier, f["wholesale_share"]) == ("review", 1.0)
    assert reasons[0] == "wholesale 100%"


def test_a_chain_can_be_suggested_its_store_count_is_not_a_vote():
    # locations and the Overture tag are context in int_screen_features; no rule reads them (the chain rule, #149,
    # decides what is public)
    products = [coffee(i, "Many Shops Coffee") for i in range(8)]
    tier, _, f = score("Many Shops Coffee", "manyshops.example", products, "40 cafes across the state.")
    assert tier == "suggested" and "locations" not in f and "place_tags" not in f


# --- each rule of section 4.2 ----------------------------------------------------------------------------------------


def test_a_multi_roaster_shop_goes_to_review():
    products = [coffee(i, v) for i, v in enumerate(["Made Up Roasters", "Roaster Two", "Third Wave Co"] * 2)]
    tier, reasons, f = score("Made Up Roasters", "madeup.example", products, "We roast in house.")
    assert (tier, f["vendors"], reasons[0]) == ("review", 3, "vendors 3")


def test_a_serves_phrase_beside_roastery_wording_is_no_doubt():
    products = [coffee(i, "Made Up Roasters") for i in range(6)]
    home = "We roast every Tuesday. We serve Made Up Roasters coffee and proudly pour Onyx Coffee Lab on guest tap."
    tier, reasons, f = score("Made Up Roasters", "madeup.example", products, home)
    assert tier == "suggested" and f["serves_phrase"] == "proudly pour Onyx Coffee Lab"


@pytest.mark.parametrize(
    ("home", "phrase"),
    [
        ("Proudly serving Portland since 2005.", None),  # a place, not a roaster
        ("We serve a full breakfast.", None),
        ("We proudly serve Passenger Coffee.", "proudly serve Passenger Coffee"),  # says coffee
        ("Featuring Corner Cafe house blend.", None),  # the store's own name
    ],
)
def test_serves_wording_counts_only_when_it_names_coffee_or_a_known_roaster(home, phrase):
    assert sr.home_signals(home, "Corner Cafe", KNOWN)["serves_phrase"] == phrase


def test_roastery_wording_is_read_from_the_home_page_never_from_products():
    products = [coffee(i, body_text="Small batch roasted by us in Portland. Roast: Light.") for i in range(6)]
    tier, _, f = score("Corner Cafe", "cornercafe.example", products, "Coffee and toast.")
    assert (f["roast_phrase"], tier) == (
        None,
        "review",
    )  # coffee for sale, but no evidence it is the store's own


def test_too_few_coffees_or_too_little_of_the_page_stays_in_review():
    few = [coffee(i, "Made Up Roasters") for i in range(4)]
    assert score("Made Up Roasters", "madeup.example", few, "We roast in house.")[0] == "review"
    diluted = [coffee(i, "Made Up Roasters") for i in range(5)] + [mug(i) for i in range(30)]
    tier, _, f = score("Made Up Roasters", "madeup.example", diluted, "We roast in house.")
    assert (tier, f["named"], round(f["coffee_share"], 2)) == ("review", 5, 0.14)


def test_coffee_with_no_bag_sizes_stays_in_review():
    products = [
        coffee(i, "Made Up Roasters", sizes=((None, "Default Title"),), body_text="Roast: Light.")
        for i in range(6)
    ]
    tier, _, f = score("Made Up Roasters", "madeup.example", products, "We roast in house.")
    assert (tier, f["bag_share"]) == ("review", 0.0)


def test_a_whole_catalog_with_no_coffee_is_dropped_but_a_full_page_goes_to_review():
    merch = [mug(i) for i in range(12)]
    assert score("Gift Shop", "gifts.example", merch, "Gifts for all.")[:2] == ("dropped", ["no_coffee 0/12"])
    tier, reasons, _ = score("Gift Shop", "gifts.example", merch, "Gifts for all.", page_full=True)
    assert (tier, reasons) == ("review", ["no_coffee 0/12", "page_full"])  # coffee may be on page 2
    tier, reasons, _ = score("Made Up Roasters", "madeup.example", merch, "We roast in house.")
    assert (tier, reasons) == ("review", ["no_coffee 0/12", "roast_words"])


def test_a_store_not_priced_in_us_dollars_is_dropped():
    products = [coffee(i, "Made Up Roasters") for i in range(6)]
    for p in products:
        p["variants"] = [v | {"currency": "CAD"} for v in p["variants"]]
    assert score("Made Up Roasters", "madeup.example", products, "We roast in house.")[:2] == (
        "dropped",
        ["currency:CAD"],
    )


@pytest.mark.parametrize(
    ("screen", "home", "expected"),
    [
        ({"status": "blocked", "refusal": "robots_disallow", "platform": ""}, None, ("not_screened", ["refused:robots_disallow"])),
        ({"status": "blocked", "refusal": "challenge", "platform": ""}, None, ("not_screened", ["refused:challenge"])),
        ({"status": "robots_unavailable", "platform": ""}, None, ("not_screened", ["no_answer:robots_unavailable"])),
        ({"status": "http_403", "platform": ""}, None, ("not_screened", ["no_answer:http_403"])),
        ({"status": "unreachable", "platform": ""}, None, ("dropped", ["dead:unreachable"])),
        ({"status": "http_404", "platform": ""}, None, ("dropped", ["dead:http_404"])),
        ({"platform": "wix?"}, "A neighborhood cafe.", ("not_screened", ["unread_platform:wix?"])),
        ({"platform": "wix?"}, "We roast in house.", ("review", ["unread_platform:wix?", "roast_words"])),
        ({"platform": "woocommerce?", "page_timeout": True}, "Hello.", ("review", ["page_timeout"])),
        ({"platform": "shopify"}, "Hello.", ("dropped", ["empty_catalog"])),
    ],
)  # fmt: skip
def test_the_rules_abstain_when_the_site_gave_no_evidence(screen, home, expected):
    assert score("A Place", "aplace.example", [], home or "", **screen)[:2] == expected


def test_every_tier_the_rules_give_is_a_known_tier():
    assert set(sr.TIERS) == {"suggested", "review", "dropped", "not_screened"}


# --- the pieces ------------------------------------------------------------------------------------------------------


def test_a_known_roasters_name_made_of_coffee_words_is_never_matched_in_a_title():
    assert (
        KNOWN.in_title("Ethiopia Single Origin Coffee", "corner") is None
    )  # a registry roaster's whole name
    assert (
        KNOWN.in_title("Onyx Monarch Espresso", "corner") is None
    )  # one distinctive word: as often a blend's name
    assert KNOWN.in_title("Counter Culture Hologram", "corner") == "counter culture"
    assert KNOWN.in_title("Counter Culture Hologram", "counter culture") is None  # its own name


def test_a_product_the_rules_call_coffee_but_that_is_not_named_as_one_is_not_counted():
    jam = coffee(
        1, title="Sugar Plum Jam", product_type="", tags=[], body_text="Sweet and bright. 12 oz jar."
    )
    s = sr.product_signals(jam, "Gift Shop", "gifts.example", KNOWN)
    assert not s["named"] and s["bag_grams"] == [] and s["vendor_key"] == ""


def test_features_match_the_dbt_models_arithmetic():
    """The same five products as dbt's unit test screen_features_and_the_latest_screen (screen s1): the two
    implementations of section 4.1 give the same numbers."""
    rows = [
        {"is_coffee": True, "named": True, "is_wholesale": False, "roast_level": "light", "origin_country": "Ethiopia",
         "bag_grams": [340, 907], "vendor_key": "made up", "own_vendor": True, "other_roaster": False, "currencies": ["USD"]},
        {"is_coffee": True, "named": True, "is_wholesale": True, "roast_level": None, "origin_country": "Ethiopia",
         "bag_grams": [340], "vendor_key": "made up", "own_vendor": True, "other_roaster": False, "currencies": ["USD"]},
        {"is_coffee": True, "named": True, "is_wholesale": False, "roast_level": "dark", "origin_country": "Peru",
         "bag_grams": [], "vendor_key": "other co", "own_vendor": False, "other_roaster": True, "currencies": ["USD"]},
        {"is_coffee": True, "named": False, "is_wholesale": False, "roast_level": None, "origin_country": None,
         "bag_grams": [], "vendor_key": "", "own_vendor": False, "other_roaster": False, "currencies": ["USD"]},
        {"is_coffee": False, "named": False, "is_wholesale": False, "roast_level": None, "origin_country": None,
         "bag_grams": [], "vendor_key": "", "own_vendor": False, "other_roaster": False, "currencies": ["USD", "CAD"]},
    ]  # fmt: skip
    f = sr.features(
        {"status": "ok", "platform": "shopify"}, rows, {"roast_phrase": "we roast", "serves_phrase": None}
    )
    assert {k: f[k] for k in ("products", "coffees", "named", "coffee_share", "bag_share", "bag_sizes", "roast_share",
                              "origins", "origin_repeat", "own_vendor", "own_vendor_share", "other_roaster", "vendors",
                              "wholesale_share", "currencies", "roast_phrase")} == {
        "products": 5, "coffees": 4, "named": 3, "coffee_share": 0.6, "bag_share": 2 / 3, "bag_sizes": [340, 907],
        "roast_share": 2 / 3, "origins": 2, "origin_repeat": 1, "own_vendor": 2, "own_vendor_share": 2 / 3,
        "other_roaster": 1, "vendors": 2, "wholesale_share": 1 / 3, "currencies": ["CAD", "USD"],
        "roast_phrase": "we roast",
    }  # fmt: skip


def test_the_candidate_rules_differ_only_in_the_suggested_tiers_thresholds():
    products = [coffee(i, "Made Up Roasters") for i in range(3)]
    f = score("Made Up Roasters", "madeup.example", products, "We roast in house.")[2]
    assert sr.tier(f)[0] == "review"
    assert sr.tier(f, sr.Rule(min_named=3))[0] == "suggested"
    no_brand = score(
        "Corner Cafe", "cornercafe.example", [coffee(i, "Corner Cafe") for i in range(6)], "Open daily."
    )[2]
    assert sr.tier(no_brand)[0] == "suggested"
    assert sr.tier(no_brand, sr.Rule(own_vendor_path=False))[0] == "review"
