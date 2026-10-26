"""Decide whether a catalog product is a bag of coffee (vs. gear, merch, subscriptions, bundles)."""

from __future__ import annotations

import re

from .models import CatalogItem
from .origins import COUNTRIES

_NOT_COFFEE = re.compile(
    r"\b(grinder|kettle|mug|mugs|tumbler|shirt|tee|t-shirt|hoodie|sweatshirt|hat|beanie|cap|tote|bag clip|"
    r"sticker|poster|gift ?card|e-?gift|subscription|subscribe|filters?|dripper|brewer|chemex|aeropress|"
    r"french press|v60|scale|machine|espresso machine|tamper|pitcher|carafe|capsules?|pods?|k-?cups?|"
    r"cold brew (can|concentrate)|concentrate|ready to drink|rtd|cans?|matcha|chai|chocolate bar|"
    r"syrup|class|workshop|wholesale|merch|apparel|candle|book|socks|bundle|sampler|variety pack|coaster|"
    r"bottle|strainer|glass|mat|towel|ornament|keychain|"
    r"tasting set|trio|flight|gift box|gift set|\bset\b|instant|steeped|drip bags?|single serve)\b",
    re.I,
)
# Strong evidence a product is beans. The bare word "coffee" is not (mugs, shirts, "Bring Me Coffee").
_COFFEE_WORDS = re.compile(
    r"\b(whole bean|single origin|single-origin|blend|espresso|decaf|roast|roasted|washed|natural|honey|"
    r"anaerobic|gesha|geisha|bourbon|typica|caturra|catuai|heirloom|sl28|pacamara|microlot|micro-lot|"
    r"tasting notes?|elevation|masl|varietal|process)\b",
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
    if _NOT_COFFEE.search(item.product_type) or re.search(r"\b(equipment|merch|gear|apparel)\b", tags, re.I):
        return False
    if _COFFEE_WORDS.search(head) or _COUNTRY.search(item.title) or _COFFEE_TYPE.search(item.product_type):
        return True
    body = item.body_text[:1500]
    evidence = bool(_COFFEE_WORDS.search(tags) or _COFFEE_WORDS.search(body) or _COUNTRY.search(body))
    # Fanciful names ("Geometry", "Perennial"): a bag-sized variant or bag wording, plus coffee language.
    return evidence and (_bag_sized(item) or bool(_BAG.search(head)) or "coffee" in head.lower())
