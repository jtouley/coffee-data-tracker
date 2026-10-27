"""Decide whether a catalog product is a bag of coffee (vs. gear, merch, subscriptions, bundles)."""

from __future__ import annotations

import re

from .models import CatalogItem
from .origins import COUNTRIES

_NOT_COFFEE = re.compile(
    r"\b(grinder|kettle|mug|mugs|tumbler|shirt|tee|t-shirt|hoodie|sweatshirt|hat|beanie|cap|tote|bag clip|"
    r"sticker|poster|gift ?card|e-?gift|subscription|subscribe|filters|coffee filter|filter bags?|kone|(paper|cone|basket) filter|filter (papers?|holders?|baskets?|cones?)|dripper|brewer|chemex|aeropress|"
    r"french press|v60|scale|machine|espresso machine|tamper|pitcher|carafe|capsules?|pods?|k-?cups?|"
    r"cold brew (can|concentrate)|concentrate|ready to drink|rtd|cans?|matcha|chai|chocolate bar|"
    r"syrup|class|workshop|wholesale|merch|apparel|candle|book|socks|bundle|sampler|variety pack|coaster|"
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
    r"\d+[- ]month (email )?gift)\b",
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


def is_coffee(item: CatalogItem) -> bool:
    head = f"{item.title} | {item.product_type}"
    tags = " ".join(item.tags)
    if _NOT_COFFEE.search(head) or _TEA.search(item.title) or _TEA_TYPE.search(item.product_type):
        return False
    title = item.title
    if (
        _PART_NO.search(title)
        or _PARTS_SELLER.search(title)
        or _TEST.search(title)
        or _TEST_CAPS.search(title)
        or _FILTER_GEAR.search(title)
    ):
        return False
    if _SAUCE.search(title) and not (_COFFEE_WORDS.search(title) or _COUNTRY.search(title)):
        return False
    if _SWEETS.search(title) and not (_SWEET_COFFEE.search(title) or _COUNTRY.search(title)):
        return False
    if _NOT_COFFEE.search(item.product_type) or re.search(r"\b(equipment|merch|gear|apparel)\b", tags, re.I):
        return False
    if _COFFEE_WORDS.search(head) or _COUNTRY.search(item.title) or _COFFEE_TYPE.search(item.product_type):
        return True
    body = item.body_text[:1500]
    evidence = bool(_COFFEE_WORDS.search(tags) or _COFFEE_WORDS.search(body) or _COUNTRY.search(body))
    # Fanciful names ("Geometry", "Perennial"): a bag-sized variant or bag wording, plus coffee language.
    return evidence and (_bag_sized(item) or bool(_BAG.search(head)) or "coffee" in head.lower())
