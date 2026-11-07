"""Decide whether a catalog product is a bag of coffee (vs. gear, merch, subscriptions, bundles)."""

from __future__ import annotations

import re

from .extract import DEMONYMS
from .models import CatalogItem
from .origins import COUNTRIES

_NOT_COFFEE = re.compile(
    r"\b(grinder|kettle|mug|mugs|tumbler|shirt|tee|t-shirt|hoodie|sweatshirt|hat|beanie|cap|tote|bag clip|"
    r"sticker|poster|gift ?card|e-?gift|filters|coffee filter|filter bags?|kone|(paper|cone|basket) filter|filter (papers?|holders?|baskets?|cones?)|dripper|brewer|chemex|aeropress|"
    r"french press|v60|scale|machine|espresso machine|tamper|pitcher|carafe|capsules?|pods?|k-?cups?|"
    r"cold brew (can|concentrate)|concentrate|ready to drink|rtd|cans?|matcha|chai|chocolate bar|"
    r"syrup|class|workshop|merch|apparel|candle|book|socks|bundle|sampler|variety pack|coaster|"
    r"bottle|strainer|glass|mat|towel|ornament|keychain|chocolate[- ]covered|gragee|dragee|"
    r"bonbons?|granola|brittle|toffee bar|"
    r"tasting set|trio|flight|gift box|gift set|\bset\b|instant|steeped|drip bags?|single serve|"
    # Machine parts, cups, cleaning supplies, services and other things sold next to beans.
    r"screws?|gaskets?|o-?rings?|burrs|burr set|portafilters?|shower screens?|steam (wands?|tips?)|"
    r"solenoids?|thermostats?|(machine|grinder|spare|replacement) parts?|lids?|java jackets?|cup sleeves?|"
    r"saucers?|(espresso|lungo|cappuccino|latte|demitasse|dosing|tasting) cups?|brush(es)?|cleaner(?! cup)|"
    r"cleaning|descal\w*|shipping protection|relief donation|gift baskets?|weekend experience|repairs?|"
    r"warranty|"
    # Syrups and sauces by brand, brewers, analysers, books, classes, tea and gift subscriptions.
    r"torani|monin|da ?vinci gourmet|ghirardelli|hollander|syrups|coffee ?makers?|moka pot|stovetop|"
    r"analy[sz]ers?|mineral drops|paperback|hardcover|handbook|playbook|classes|toll roasting|pekoe|"
    r"raw (local )?honey|cocoa powder|pur[eé]e|smoothies?( mix)?|coffee server|recurring|installments|"
    r"\d+[- ]month (email )?gift|gift bags?|jute|ecopours?|pre-filled|chocolate (covered )?(espresso|coffee) beans|"
    r"pistachios?|chestnuts|cashews?|knock ?box|tamp(ing)? station|enamel pins?|hot cups?|"
    r"paper cups?|gift collection|handle collection|teamaker|sachets?)\b|^donate\b",
    re.I,
)
# Sauces and sweets, unless the title also reads as a coffee: "Ecuador El Sauce" is a farm, "Survival Sauce" a
# blend, "Sugar Cookie Decaf" a flavored coffee and "Candy-Natural-Processed Catuai" a lot.
_SAUCE = re.compile(r"\bsauces?\b", re.I)
# Paper and reusable filters, filter kits and case packs; "Filter Blend" and "Filter Packs" of coffee stay.
_FILTER_GEAR = re.compile(
    r"\b(reusable|paper|cellulose|cloth)\b(\W+\w+){0,2}\W+filters?\b|\bfilters? (kit|tool)s?\b|"
    r"\bfilters?,? \d+\s*(vnt|ct|count|pcs)\b|\d+/case\b|\bburlap sacks?\b|\bstarter kit\b",
    re.I,
)
_SWEETS = re.compile(r"\b(cand(y|ies)|truffles?|cookies?)\b", re.I)
# Drinks and other food, unless the title also names a bag of beans: "Cold Brew Blend" and "Cold Brew Filter Packs"
# stay, "Classic Black Cold Brew" (cans), "Espresso Tonic" and "Wild Honey Latte" go.
_DRINKS = re.compile(
    r"\b(cold brew|nitro|tonic|soda|latte|lemonade|energy|creamer|horchata|nuts|almonds?|patties|pins?|"
    r"traveler|pour ?overs?|gal(lon)?s?|canned|cappuccino|frappe|kits?)\b",
    re.I,
)
_BEANS = re.compile(
    r"\b(blend|roast|whole bean|ground|beans|decaf|single[- ]origin|filter packs?|coarse|flavou?red)\b", re.I
)
# A subscription is named as one; a coffee that can also be bought on subscription is still a coffee.
_SUBSCRIPTION = re.compile(r"\b(subscriptions?|subscribe)\b", re.I)
_SUBSCRIPTION_TYPE = re.compile(r"^\s*subscriptions?\s*$", re.I)
# Green (unroasted) coffee is sold for home roasting; the catalog lists roasted coffee only.
_GREEN_TITLE = re.compile(r"\b(green (coffee|beans?)|unroasted|raw coffee)\b|^green\b", re.I)
_GREEN_META = re.compile(r"\b(green( coffee| beans?)?|unroasted)\b|^unr\b", re.I)
_TEA_META = re.compile(r"\b(teas?|herbal|tisane)\b", re.I)
_NOT_COFFEE_TYPE = re.compile(r"\b(merchandise|drinkware|training|events?|powders?|accessor(y|ies))\b", re.I)
_DEMONYM = re.compile(r"\b(" + "|".join(DEMONYMS) + r")\b", re.I)
# Wholesale bags and case packs are coffee, but their prices aren't comparable with retail bags.
# Wholesale: sold to cafes, offices and resellers. "Whole sale" and fraction packs (portion packs for commercial
# brewers) were measured on the real catalog: eval/wholesale/README.md.
_WHOLESALE = re.compile(
    r"\b(wholesale|whole sale|resale|b2b|food ?service|frac(tion)?[- ]?packs?)\b|\bcase (of )?\(?\d+\)?"
    r"|\bcases? of \d+",
    re.I,
)
# A tag marks the listing itself ("Wholesale", "wholesale-only", "B2B Exclusive"), not "wholesale-available".
_WHOLESALE_TAG = re.compile(r"(wholesale|resale|b2b|food ?service)([-_ ](only|exclusive))?", re.I)
_OTHER_META = re.compile(
    r"\b(equipment|merch|gear|apparel|parts|gift sets?|instants?|samplers?|bundles?)\b", re.I
)
# A tag counts only when it is the whole tag, with at most one word in front ("Brewing Equipment", "Green Coffee"):
# stores tag real coffees "Green Apple", "tea-like", "herbal", "Instant Gratification" or "bundle-flat-discount:1.25".
_TAG = re.compile(
    r"(\w+ )?(equipment|merch(andise)?|gear|apparel|parts|gift sets?|instants?|samplers?|bundles?|"
    r"green( coffee| beans?)?|unroasted|(loose[- ]leaf )?teas?|herbal teas?|tisanes?)",
    re.I,
)
_SWEET_COFFEE = re.compile(
    r"\b(flavou?red|coffee|roast(ed)?|decaf|blend|espresso|single[- ]origin|washed|natural|anaerobic|processed)\b",
    re.I,
)
# Spare parts and machines: part numbers like "(VITE3X10)", "(T.1.054)", "(62100000093)" or "Ceado (40315)", a
# "| Fits Linea Mini" suffix, or a machine, grinder or cleaner maker's name first. Part numbers are matched
# case-sensitively, so bag sizes like "(12oz.)" and "(2-Pack)" don't count.
_PART_NO = re.compile(
    r"\(\d{5,}(\.\d+)?\)|\((?=[A-Z0-9./-]*\d)(?=[A-Z0-9./-]*[A-Z])(?![\d.]+(LBS?|OZ|G|KG)\))[A-Z0-9][A-Z0-9./-]{4,}\)"
)
_PARTS_SELLER = re.compile(
    r"\|\s*fits\s|^(BFC|La Marzocco|Mahlk[oö]nig|Mazzer|Ceado|Anfim|Bentwood|Rancilio|Etzinger|PUQ ?Press|Egro|"
    r"Nuova Simonelli|Victoria Arduino|Baratza|Fiorenzato|Compak|Breville|Lelit|Profitec|La Spaziale|Faema|Gaggia|"
    r"Bezzera|Synesso|Ditting|Fetco|Urnex|Cafiza|Pallo|Rattleware|Acaia|Hario|Kalita|Comandante|Timemore|"
    r"1Zpresso|Link Roaster|Modbar)\b",
    re.I,
)
# Test listings: "[Test] Rafael", "Star of the North - TEST", "TEST BATCH 2 LB", "E2E Test". A trailing mixed-case
# "Test" ("Gold Miners Decaf Test") or a name containing TEST ("NLU THE EYE TEST") stays.
_TEST = re.compile(r"[\[(]test[\])]|\be2e\b|\btest (coffee|product|batch|item)\b", re.I)
_TEST_CAPS = re.compile(r"^TEST\b|[-–|:]\s*TEST\s*$")
# Strong evidence a product is beans. The bare word "coffee" is not (mugs, shirts, "Bring Me Coffee").
_COFFEE_WORDS = re.compile(
    r"\b(whole bean|single origin|single-origin|blend|espresso|decaf|roast|roasted|washed|natural|honey|"
    r"anaerobic|gesha|geisha|bourbon|typica|caturra|catuai|heirloom|sl28|pacamara|microlot|micro-lot|"
    r"tasting notes?|elevation|masl|varietal|process|flavou?red coffee)\b",
    re.I,
)
# "Tea" only counts against a product named or typed as tea, so "Coffee & Tea" stores and "Cup of Excellence"
# lots are not excluded.
_TEA = re.compile(r"\b(tea|teas|tisane|herbal)\b(?!.*\bcoffee\b)", re.I)
_TEA_TYPE = re.compile(r"^\s*(loose[- ]leaf )?teas?\s*$", re.I)
_COFFEE_TYPE = re.compile(r"^\s*(whole[- ]bean |roasted |specialty )?coffees?( beans?)?\s*$", re.I)
_BAG = re.compile(r"\b(\d+(\.\d+)?\s*(oz|lb|lbs|g|kg)|bag)\b", re.I)
_COUNTRY = re.compile(r"\b(" + "|".join(re.escape(c) for c in COUNTRIES) + r")\b", re.I)


def _bag_sized(item: CatalogItem) -> bool:
    return any(v.grams and 150 <= v.grams <= 2500 for v in item.variants)


def is_wholesale(item: CatalogItem) -> bool:
    return bool(
        says_wholesale(f"{item.title} | {item.product_type}")
        or any(_WHOLESALE_TAG.fullmatch(t.strip()) for t in item.tags)
    )


def says_wholesale(text: str) -> bool:
    """Whether a listing's title, or a bag's label, reads as wholesale ("Wholesale - 5LB", "Case of 6")."""
    return bool(_WHOLESALE.search(text))


def exclusion(item: CatalogItem) -> str | None:
    """The first rule that excludes the product outright (never a coffee, whatever else the listing says: gear,
    parts, merch, tea, green coffee, subscriptions, test listings), by name, or None. The rules are checked in a
    fixed order, so a product that more than one rule excludes always gets the same name."""
    title, ptype = item.title, item.product_type
    checks = (
        ("not_coffee_words", lambda: _NOT_COFFEE.search(f"{title} | {ptype}")),
        ("tea_title", lambda: _TEA.search(title)),
        ("tea_type", lambda: _TEA_TYPE.search(ptype)),
        ("part_number", lambda: _PART_NO.search(title)),
        ("parts_seller", lambda: _PARTS_SELLER.search(title)),
        ("test_listing", lambda: _TEST.search(title) or _TEST_CAPS.search(title)),
        ("filter_gear", lambda: _FILTER_GEAR.search(title)),
        ("subscription", lambda: _SUBSCRIPTION.search(title) or _SUBSCRIPTION_TYPE.search(ptype)),
        ("green_coffee", lambda: _GREEN_TITLE.search(title) or _GREEN_META.search(ptype)),
        ("not_coffee_type", lambda: _NOT_COFFEE_TYPE.search(ptype)),
        ("tea_type", lambda: _TEA_META.search(ptype) and not re.search(r"\bcoffee\b", ptype, re.I)),
        ("other_type", lambda: _OTHER_META.search(ptype)),
        ("tag", lambda: any(_TAG.fullmatch(t.strip()) for t in item.tags)),
    )
    return next((name for name, check in checks if check()), None)


def excluded(item: CatalogItem) -> bool:
    """Never a coffee, whatever else the listing says (exclusion names the rule). These skip Claude's verdict
    (verdict.py)."""
    return exclusion(item) is not None


def is_coffee(item: CatalogItem) -> bool:
    """The rules' answer: used where Claude has no verdict, and as the floor the labeled sets hold it to."""
    return decision(item)[0]


def decision(item: CatalogItem) -> tuple[bool, str]:
    """The rules' answer and the rule that gave it: excluded:<rule> (exclusion), sauce, sweets or drinks (not
    coffee); coffee_words, country, demonym or coffee_type (coffee); else signals:<n>, the count of the four
    independent signals (coffee at two or more). Only an explanation: is_coffee is this answer."""
    if (why := exclusion(item)) is not None:
        return False, f"excluded:{why}"
    title, ptype = item.title, item.product_type
    head = f"{title} | {ptype}"
    tags = " ".join(item.tags)
    meta = f"{ptype} | {tags}"
    if _SAUCE.search(title) and not (_COFFEE_WORDS.search(title) or _COUNTRY.search(title)):
        return False, "sauce"
    if _SWEETS.search(title) and not (
        _SWEET_COFFEE.search(title) or re.search(r"\bflavou?red\b", meta, re.I) or _COUNTRY.search(title)
    ):
        return False, "sweets"
    if _DRINKS.search(title) and not _BEANS.search(title):
        return False, "drinks"
    for name, hit in (
        ("coffee_words", lambda: _COFFEE_WORDS.search(head)),
        ("country", lambda: _COUNTRY.search(title)),
        ("demonym", lambda: _DEMONYM.search(title)),
        ("coffee_type", lambda: _COFFEE_TYPE.search(ptype)),
    ):
        if hit():
            return True, name
    body = item.body_text[:1500]
    # Otherwise two independent signals: coffee language in the tags or description, an origin there, the word
    # coffee in the type, tags or description, a bag-sized variant, or bag wording in the title.
    signals = [
        bool(_COFFEE_WORDS.search(tags) or _COFFEE_WORDS.search(body)),
        bool(_COUNTRY.search(body) or _DEMONYM.search(body)),
        bool(re.search(r"\bcoffees?\b", f"{title} {meta} {body}", re.I)),
        _bag_sized(item) or bool(_BAG.search(head)),
    ]
    n = sum(signals)
    return n >= 2, f"signals:{n}"
