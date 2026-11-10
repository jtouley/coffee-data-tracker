"""Bag weights (grams) read again, at build time, from text the lake already stores.

The crawler reads a variant's weight from its own title only (models.parse_grams), so a store that names the size
in the product title ("Bolivian - 1 lb"), in a slug-style option ("1-lb-custom-whole-bean") or in its description
("12 oz bag") stores no weight, and its coffee gets no price per 100 g. dbt's int_variant_grams runs
`product_grams` over every stored product version; dim_coffee reads its grams instead of the stored ones.

The rules abstain rather than guess: a weight is used only when the text states one weight (or several that agree),
nothing in it says the listing is a pack, a sampler, pods or a choice of sizes, and, for a weight the product title
or description states, the options without a weight of their own cost the same (so size isn't what they vary).
Abstaining leaves the coffee without a price per 100 g, as before. eval/bag_grams measures the rules.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import parse_grams

# The weights models.parse_grams keeps (30 g to 25 kg); anything outside is a misread.
MIN_G, MAX_G = 30, 25_000
# Options whose prices differ by more than this share are taken to be different sizes.
SAME_PRICE = 1.15
# Two weights that differ by less than this share are the same statement in two units ("12 oz (340 g)").
AGREE = 0.02

_NUM = (
    r"(?P<num>(?P<whole>\d+)\s+(?P<fn>[1-3])/(?P<fd>[248])"  # 1 1/2
    r"|(?P<n>\d+)/(?P<d>\d+)"  # 1/2, written without spaces: "No. 4 / 5 lbs" is grind 4, 5 lb
    r"|\d+(?:\.\d+)?|\.\d+"
    r"|[½¼¾]|half|quarter|one|two|three|four|five)"
)
_UNIT = r"(?P<unit>kilo(?:gram)?s?|kgs?|lbs?|pounds?|ounces?|ozs?|onz|grams?|grs?|gms?|g)\b"
# A number, then a unit ("12 oz", "1-lb", "12_ounces"), or "#" (5# and "2.5 # bag" are pounds; "#5" is not).
_WEIGHT = re.compile(rf"(?<![\w/.,]){_NUM}(?:\s*[-_]?\s*{_UNIT}|\s?(?P<hash>#)(?![\w#]))", re.I)
_FACTOR = {"k": 1000.0, "l": 453.592, "p": 453.592, "o": 28.3495, "g": 1.0, "#": 453.592}
_WORDS = {
    "½": 0.5,
    "¼": 0.25,
    "¾": 0.75,
    "half": 0.5,
    "quarter": 0.25,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}
# "2 x 12 oz", "12oz x 2", "two 16 oz": the listing is that many bags.
_COUNT = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_TIMES_BEFORE = re.compile(r"(?:(?<![\w.])(\d{1,2})\s*[x×]|\b(two|three|four|five|six)\b\(?)\s*$", re.I)
# Any count of bags in a description ("50 x 4oz bags") is about something other than this listing's bag.
_TIMES = re.compile(r"(?<![\w.])\d{1,3}\s*[x×]\s*\d|\b(?:two|three|four|five|six)\s+\d", re.I)
_TIMES_GLUED = re.compile(r"(\d)\s*([x×])(?=\d)", re.I)
_TIMES_AFTER = re.compile(r"^\s*(?:bags?\s*)?[x×]\s*(\d{1,2})(?![\w.])", re.I)
# More than one bag, or not a bag weight: a weight next to these isn't what the price buys ("12 oz (pack of 6)",
# "3 CT", "22/4ounce packs", "12 fl oz"). Checked in every text, an option's title included.
_PACK = re.compile(
    r"\b(?:\d+\s*[-_ ]?\s*(?:pack|pk|ct|count)s?\b|packs? of|case|cases|set of|box of|bundle|"
    r"variety\s+(?:pack|box|set)|flight|trio|duo|subscriptions?|monthly|per\s+month|fl\.?\s*oz|fluid|ml\b|cans\b|"
    r"bottles?|\d+\s*/\s*\d+(?:\.\d+)?\s*-?\s*(?:oz|ounce|g)\w*\s*(?:packs?|packets?|bags?|pouch)|"
    r"(?<!no\.\s)(?<!no\s)(?<!#)(?<![\d.])\d{1,2}\s+/\s+\d+(?:\.\d+)?\s*-?\s*(?:lb|pound|oz|ounce|kg|g\b))",
    re.I,
)
# A product that isn't sold as a bag of beans or grounds (pods, capsules, portion packets, steeped or drip bags,
# green coffee), or whose title or description may be about something else (a sampler of several coffees, a sample,
# a roast batch). Checked in a product's title and description only: an option such as "12 oz / Reusable K-Cup" is
# a grind for a refillable filter, "4 oz sample" one small bag, "Batch Brew" a grind.
_NOT_BEANS = re.compile(
    r"\b(?:k[-\s]?cups?|pods?|capsules?|sachets?|packets?|portion|single[-\s]?serve|steep(?:ed)?\s*bags?|"
    r"drip\s*bags?|tea|samplers?|samples?|batch(?:es)?|double\s+shot|latte|americano|cappuccino|cortado|"
    r"brewed\s+coffee|by\s+the\s+cup|green\s*(?:coffee|beans?)|unroasted)\b",
    re.I,
)
# A description that offers several sizes: one weight in it doesn't say which this option is.
_SEVERAL_SIZES = re.compile(
    r"\b(?:also|available|availability|offered|options?|sizes|choose|choice|select|upgrade)\b",
    re.I,
)
# A weight that is the size of a lot or a roast run, not of a bag ("Limited to 30 lbs!", "a run of 50 x 4oz bags",
# "only 20 lbs this year"): a description saying so gives no weight.
# Shipping terms ("Orders over 2 pounds ship free") and ranges ("a 20-30 pound ruck sack") aren't a bag either.
_LOT = re.compile(
    r"\b(?:limited|run\s+of|harvest|crop|lots?\b|this\s+year|in\s+total|total\s+of|only\s+\d|ship(?:s|ping|ped)?\b|"
    r"orders?\b|over\s+\d|\d+\s*(?:-|to|–)\s*\d+\s*(?:lb|pound|oz|ounce|kg|g\b))",
    re.I,
)
# "Available in 12oz only", "Only available in 2 pound bags": one size after all.
_ONLY = re.compile(
    r"\bonly\s+(?:available|sold|comes?)\s+in\b|\bavailable\s+(?:only\s+)?in\s+[^\n,;]{0,25}\bonly\b", re.I
)
# A weight about brewing, not the bag ("20 g of coffee", "per 12 oz of water"): ignored in a description.
_BREWING = re.compile(
    r"\b(?:water|liquid|milk|cup|cups|mug|per|ratio|dose|dosed|shots?|brew|brewed|brewing|drinks?|latte|iced|"
    r"serving|servings|tbsp|"
    r"tbs|tablespoons?|scoops?|grind|yield|roast(?:er|ed|ing)?\s+in|roaster|drum)\b",
    re.I,
)


def _value(m: re.Match[str]) -> float | None:
    if m.group("whole"):
        num = int(m.group("whole")) + int(m.group("fn")) / int(m.group("fd"))
    elif m.group("n"):
        d = int(m.group("d"))
        if d == 0 or int(m.group("n")) >= d:  # "12/16 oz" is not a fraction of a unit
            return None
        num = int(m.group("n")) / d
    else:
        raw = m.group("num").lower()
        num = _WORDS[raw] if raw in _WORDS else float(raw)
    unit = (m.group("unit") or "#").lower()
    return num * _FACTOR[unit[0]]


def _norm(text: str | None) -> str:
    """'2x1 lb' as '2 x 1 lb', so the count and the weight read apart."""
    return _TIMES_GLUED.sub(r"\1 \2 ", text or "")


def mentions(text: str) -> list[tuple[float, int, int]]:
    """Every weight the text states, in grams, with its span; an explicit count multiplies ("2 x 12 oz" and
    "2x1 lb" are two bags)."""
    text = _norm(text)
    out = []
    for m in _WEIGHT.finditer(text or ""):
        g = _value(m)
        if g is None:
            continue
        before, after = _TIMES_BEFORE.search(text[: m.start()]), _TIMES_AFTER.search(text[m.end() :])
        if before or after:
            n = (before or after).group(1) or before.group(2)  # type: ignore[union-attr]
            g *= _COUNT.get(n.lower(), 0) or int(n)
        out.append((g, m.start(), m.end()))
    return out


def _one(values: Sequence[float]) -> int | None:
    """The weight several statements agree on, or None when they disagree or it's out of range."""
    if not values:
        return None
    first = values[0]
    if any(abs(v - first) > AGREE * first for v in values):
        return None
    g = round(first)
    return g if MIN_G <= g <= MAX_G else None


def text_grams(text: str, beans_only: bool = True) -> int | None:
    """The one bag weight a title or an option states: None when it states none, several that disagree, or reads as
    more than one bag (a pack, a case, a sampler, fluid ounces). beans_only: also None for pods, capsules and the
    like (a product's title; an option's title isn't checked for these)."""
    if not text or _PACK.search(text) or (beans_only and _NOT_BEANS.search(text)):
        return None
    return _one([g for g, _, _ in mentions(text)])


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j


def body_grams(text: str) -> int | None:
    """The one bag weight a description states. Weights about brewing are ignored; a description that offers
    several sizes, or reads as a pack, gives none."""
    if not text:
        return None
    text = _norm(text)
    kept = []
    for g, a, b in mentions(text):
        # the mention's own line, up to 40 characters either side
        near = text[max(0, a - 40, text.rfind("\n", 0, a) + 1) : min(b + 40, _line_end(text, b))]
        if _PACK.search(near) or _NOT_BEANS.search(near) or _LOT.search(near) or _TIMES.search(near):
            return None
        if _SEVERAL_SIZES.search(near) and not _ONLY.search(near):
            return None
        if _BREWING.search(near):
            continue
        kept.append(g)
    return _one(kept)


def product_grams(
    title: str | None,
    body_text: str | None,
    variants: Sequence[tuple[str | None, float | None, int | None]],
) -> list[int | None]:
    """Each variant's grams, from (option title, price, stored grams) and the product's title and description.

    1. An option's own title is read again with these rules where its stored weight came from that title
       (models.parse_grams), so a misread ("1/2 lb" as 907 g, "5x 1lb" as 454 g) is corrected or dropped. A stored
       weight from elsewhere (Shopify's variant grams, a generic store's product name) is kept.
    2. When no option has a weight, the weight the product title states, else the description's, is every
       option's: only if the options cost the same (within SAME_PRICE), so they differ by grind, not size."""
    out: list[int | None] = []
    for vt, _, stored in variants:
        if stored is not None and parse_grams(vt or "") != stored:
            out.append(stored)  # not read from the option's title: Shopify's grams, a generic product's name
            continue
        g = text_grams(vt or "", beans_only=False)
        # the same weight to within rounding ("5 lbs (2.27 kg)": 2270 or 2268) keeps the stored value
        out.append(
            stored if stored is not None and g is not None and abs(g - stored) <= AGREE * stored else g
        )
    if any(g is not None for g in out) or not variants:
        return out
    prices = [p for _, p, _ in variants if p is not None and p > 0]
    if not prices or max(prices) > SAME_PRICE * min(prices):
        return out
    title = title or ""
    if _PACK.search(title) or _NOT_BEANS.search(title):
        return out
    g = text_grams(title) if mentions(title) else body_grams(body_text or "")
    return [g] * len(variants)
