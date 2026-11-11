"""Bag weights (grams) read again, at build time, from text the lake already stores.

The crawler reads a variant's weight from its own title only (models.parse_grams), so a store that names the size
in the product title ("Bolivian - 1 lb"), in a slug-style option ("1-lb-custom-whole-bean"), in a size tag or in
its description ("12 oz bag") stores no weight, and its coffee gets no price per 100 g; and a weight it did store
can be a misread ("1/2 lb" as 907 g) or not stated anywhere in the listing (Shopify's variant grams, often the
shipping weight). dbt's stg_variant_grams runs `product_grams` over each coffee's latest product version;
dim_coffee prices bags with its grams instead of the stored ones.

The grams are what the option's price buys: a pack with an explicit count is its total ("4 x 12 oz", "12oz - 3
Pack", "Case of 6 - 10 oz. bags", "48/2 oz" portion packs), and a subscription option is one delivery. The rules
abstain rather than guess: a weight is used only when the text states one weight (or several that agree), nothing
in it says the listing is a pack without a count, a bundle, pods, a drink or a choice of sizes, and, for a weight the
product title, tags or description states, the options without a weight of their own cost the same (so size isn't
what they vary). Abstaining leaves the coffee without a price per 100 g. eval/bag_grams measures the rules.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

from .models import parse_grams

# The weights models.parse_grams keeps (30 g to 25 kg); anything outside is a misread.
MIN_G, MAX_G = 30, 25_000
# Options whose prices differ by more than this share are taken to be different sizes.
SAME_PRICE = 1.15
# Two weights that differ by less than this share are the same statement in two units ("12 oz (340 g)").
AGREE = 0.02
# The most bags a count multiplies by ("Case Pack (8)", "PP 42ct", "48/2 oz").
MAX_COUNT = 99

_FRACTIONS = {2, 3, 4, 8, 16}
_NUM = (
    r"(?P<num>(?P<whole>\d+)[\s-]+(?P<fn>\d{1,2})/(?P<fd>\d{1,2})"  # 1 1/2, 1-1/3
    r"|(?P<n>\d{1,2})/(?P<d>\d{1,2}(?:\.\d+)?)"  # 1/2; or a count, "48/2 oz", "6/12 oz."
    r"|(?P<sa>\d)-(?P<sb>\d)(?=-)"  # slug decimals: "0-5-lb", "2-2-pounds"
    r"|\d+(?:\.\d+)?|\.\d+"
    r"|[½¼¾]|half|quarter|one|two|three|four|five)"
)
_UNIT = r"(?P<unit>kilo(?:gram)?s?|kgs?|lbs?|pounds?|ounces?|ozs?|onz|grams?|grs?|gms?|g)(?![a-z])"
# A number, then a unit ("12 oz", "1-lb", "12_ounces", "5lb15"), or "#" (5# and "2.5 # bag" are pounds; "#5" is
# not). A "/" or "," may come right before the number ("Whole Bean/12oz", "Whole Bean,1lb"), but not a digit's
# thousands comma ("1,000g") or a decimal point.
_WEIGHT = re.compile(rf"(?<![\w.])(?<!\d,){_NUM}(?:\s*[-_]?\s*{_UNIT}|\s?(?P<hash>#)(?![\w#]))", re.I)
_FACTOR = {"k": 1000.0, "l": 453.592, "p": 453.592, "o": 28.3495, "g": 1.0, "#": 453.592}
_WORDS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "half": 0.5, "quarter": 0.25, "one": 1, "two": 2, "three": 3}
_WORDS |= {"four": 4, "five": 5}
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

# An explicit count of bags, right before a weight: "4 x 12 oz", "20/2.00oz", "8 / 1 lb", "Case of 6 - 10 oz",
# "6 count case (12 oz", "Variety 3 Pack - 12oz", "(TWO 16 oz)". A grind or blend number isn't one ("No. 4 / 5 lbs").
_COUNT_BEFORE = [
    re.compile(r"(?<![\w.])(\d{1,3})\s*[x×]\s*$", re.I),
    re.compile(r"(?<![\w.#/])(?<!no\.\s)(?<!no\s)(?<!no\.)(?<!#\s)(\d{1,3})\s*/\s*$", re.I),
    re.compile(r"\b(?:case|pack|box)\s+of\s+(\d{1,3})\s*[-–:,(]*\s*$", re.I),
    re.compile(r"(?<![\w.])(\d{1,3})\s*[-\s]?(?:count|ct|packs?|pk)\s*(?:case|box)?\s*[-–:,(]*\s*$", re.I),
    re.compile(r"\(\s*(two|three|four|five|six)\s+$", re.I),
]
# ... and right after it: "12oz x 2", "10 oz Tin x 2", "12oz - 3 Pack", "1.5 oz 12 Pack", "2.5 oz, PP 42ct",
# "5lbs Case Pack (8)", "12 oz (pack of 6)", "12 oz bags x 3 CT".
_COUNT_AFTER = [
    re.compile(r"^\.?\s*(?:bags?|tins?|cans?|jars?|pouch(?:es)?|packs?)?\s*[x×]\s*(\d{1,3})(?![\w.])", re.I),
    re.compile(r"^[\s.,\-–(]*(?:bags?\s*)?(?:pp\s*)?(\d{1,3})\s*-?\s*(?:packs?|pk|ct|count)\b", re.I),
    re.compile(r"^\.?\s*(?:bags?\s*)?case\s*pack\s*\(\s*(\d{1,3})\s*\)", re.I),
    re.compile(r"^\.?\s*(?:bags?\s*)?[(\[,\-–]?\s*(?:pack|case|box|bundle)\s+of\s+(\d{1,3})\b", re.I),
]
_TIMES_GLUED = re.compile(r"(\d)\s*([x×])(?=\d)", re.I)
# "sold by the pound", "Blend by the lb.": one pound.
_BY_THE_POUND = re.compile(r"\bby\s+the\s+(?:pound|lb)\b\.?", re.I)

# More than one bag, or more than coffee: without an explicit count of bags, the weight isn't what the price buys
# ("12 oz (pack of 6)" has one; "Bundle", "12oz + Mug", "3 Month Gift" don't). Checked in every text.
_PACK = re.compile(
    r"\b(?:(?<!portion\s)packs?|pk|ct|count|cases?|bundles?|set\s+of|box\s+of|variety|flight|trio|duo|gift\s*(?:set|box)|"
    r"prepaid|\d+\s*months?|half\s*(?:and|&)\s*half)\b|"
    r"\+\s*(?:a\s+|free\s+)?(?:mug|cup|tumbler|shirt|tee|hat|tote|sticker|gift|grinder|book)|"
    r"\bwith\s+(?:a\s+)?(?:mug|tumbler|grinder)\b",
    re.I,
)
# Not coffee by weight: fluid ounces, concentrates, canned or bottled drinks. Checked in every text ("Nitro Cold
# Brew 12oz Can", "Cold Brew Concentrate 32 oz"); a coffee sold in a can ("1 lb. Can / Drip") is still a bag weight.
_DRINK = re.compile(
    r"\b(?:fl\.?\s*oz|fluid|ml|concentrate|nitro|ready[-\s]?to[-\s]?drink|rtd|canned|bottles?|"
    r"cold\s*brew\s+(?:cans?|bottles?)|cans?\s+of\s+cold\s*brew)\b",
    re.I,
)
# A drink sold by the cup ("12 oz Latte", "Espresso Double Shot"). Checked in a product's and an option's title, not
# in a description ("great as a latte").
_DRINK_NAMES = re.compile(
    r"\b(?:latte|americano|cappuccino|cortado|double\s+shot|brewed\s+coffee|by\s+the\s+cup)\b", re.I
)
# A can with no weight of its own ("Single Can"): a stored weight on it isn't kept unless the listing states it.
_CAN = re.compile(r"\bcans?\b", re.I)
# Not a bag of beans or grounds, or a title that may be about something else. Checked in a product's title, tags
# and description, not in an option ("12 oz / Reusable K-Cup" is a grind for a refillable filter, "4 oz sample" one
# small bag, "Batch Brew" a grind). A portion packet that states its weight is one ("A 1.5 oz packet").
_NOT_BEANS = re.compile(
    r"\b(?:k[-\s]?cups?|pods?|capsules?|sachets?|single[-\s]?serve|steep(?:ed)?\s*bags?|drip\s*bags?|instant|"
    r"samplers?|green\s*(?:coffee|beans?)|unroasted)\b",
    re.I,
)
# Only in a product's title: a roast batch's size ("Inti Honey - 20lb batch", not "Small Batch"), tea.
_TITLE_ONLY = re.compile(r"(?<!small\s)(?<!small-)\bbatch(?:es)?\b|\btea\b|\bchai\b", re.I)
# A description that offers several sizes: one weight in it doesn't say which this option is.
_SEVERAL_SIZES = re.compile(
    r"\b(?:also|available|availability|offered|options?|sizes|choose|choice|select\s+(?:a|your|from)|upgrade)\b",
    re.I,
)
# A weight that is the size of a lot, a shipment or a range, not of a bag ("Limited to 30 lbs!", "only 20 lbs this
# year", "Orders over 2 pounds ship free", "a 20-30 pound ruck sack"): a description saying so gives no weight.
_LOT = re.compile(
    r"\b(?:limited|run\s+of|harvest|crop|lots?\b|this\s+year|in\s+total|total\s+of|only\s+\d|ship(?:s|ping|ped)?\b|"
    r"orders?\b|over\s+\d|\d+\s*(?:-|to|–)\s*\d+\s*(?:lb|pound|oz|ounce|kg|g\b))",
    re.I,
)
# "Available in 12oz only", "Only available in 2 pound bags": one size after all.
_ONLY = re.compile(
    r"\bonly\s+(?:available|sold|comes?)\s+in\b|\bavailable\s+(?:only\s+)?in\s+[^\n,;]{0,25}\bonly\b", re.I
)
# A weight about brewing or roasting, not the bag ("20 g of coffee", "per 12 oz of water", "roasted in 12 kg
# batches"): ignored in a description. A decaf's "Swiss Water Process" is not water.
_BREWING = re.compile(
    r"\b(?:water|liquid|milk|cup|cups|mug|per|ratio|dose|dosed|shots?|brew|brewed|brewing|drinks?|latte|iced|"
    r"serving|servings|tbsp|tbs|tablespoons?|scoops?|grind|yield|roast(?:er|ed|ing)?\s+in|roaster|drum|"
    r"batch(?:es)?)\b",
    re.I,
)
_WATER_PROCESS = re.compile(r"\b(?:swiss|mountain|mtn\.?)\s+water\b|\bwater[-\s]+process(?:ed)?\b", re.I)
# Tags that name a size ("12oz", "Size: 5 lb", "bag-size_1lb"); a tag that is a whole sentence isn't read.
_TAG_MAX = 30

# Bag sizes a store sells by (grams): a stored weight the listing never states is kept only when it is one of these.
_STANDARD = sorted(
    {round(oz * 28.3495) for oz in (2, 3, 4, 5, 6, 8, 10, 11, 12, 14, 16, 20, 24, 32, 40, 48, 80)}
    | {round(lb * 453.592) for lb in (1, 1.5, 2, 2.5, 3, 4, 5, 10)}
    | {100, 125, 150, 200, 250, 300, 350, 400, 454, 500, 750, 1000, 2000, 2500}
)

Unsupported = Literal["standard", "drop", "keep"]


class _Ambiguous(Exception):
    """A weight that can't be read one way: "4/5 lb", "1 2/5 lb"."""


class Mention(NamedTuple):
    grams: float  # what the price buys: the bag's weight times any explicit count
    bag: float  # one bag's weight
    counted: bool
    start: int
    end: int


def _norm(text: str | None) -> str:
    """'2x1 lb' as '2 x 1 lb', so the count and the weight read apart."""
    return _BY_THE_POUND.sub("by the 1 lb", _TIMES_GLUED.sub(r"\1 \2 ", text or ""))


def _value(m: re.Match[str]) -> tuple[float, int]:
    """(one bag's grams, a count the token itself gives): "1/2 lb" is (227, 1); "48/2 oz" is (57, 48)."""
    unit = (m.group("unit") or "#").lower()
    factor = _FACTOR[unit[0]]
    count = 1
    if m.group("whole"):
        fn, fd = int(m.group("fn")), int(m.group("fd"))
        if fd not in _FRACTIONS or fn >= fd:
            raise _Ambiguous(m.group(0))
        num = int(m.group("whole")) + fn / fd
    elif m.group("n"):
        n, d = int(m.group("n")), float(m.group("d"))
        if d.is_integer() and n < d and int(d) in _FRACTIONS:
            num = n / d
        elif n >= d or (unit[0] in "og" and d >= 8):  # "48/2 oz", "20/2.00oz", "6/12 oz.": a count of bags
            num, count = d, n
        else:
            raise _Ambiguous(m.group(0))  # "4/5 lb": four fifths, or 4 and 5 lb?
    elif m.group("sa"):
        num = int(m.group("sa")) + int(m.group("sb")) / 10
    else:
        raw = m.group("num").lower()
        num = _WORDS[raw] if raw in _WORDS else float(raw)
    return num * factor, count


def _count(text: str, start: int, end: int) -> int | None:
    before, after = text[max(0, start - 30) : start], text[end : end + 30]
    for p in _COUNT_BEFORE:
        if m := p.search(before):
            return _COUNT_WORDS.get(m.group(1).lower()) or int(m.group(1))
    for p in _COUNT_AFTER:
        if m := p.search(after):
            return int(m.group(1))
    return None


def mentions(text: str) -> list[Mention]:
    """Every weight the text states, in grams, with its span (in _norm(text)); an explicit count multiplies. Raises
    _Ambiguous for a weight that reads two ways."""
    text = _norm(text)
    out = []
    for m in _WEIGHT.finditer(text):
        bag, own = _value(m)
        n = _count(text, m.start(), m.end())
        if own > 1 and n:
            raise _Ambiguous(m.group(0))
        n = own if own > 1 else n
        if n is not None and not 1 <= n <= MAX_COUNT:
            raise _Ambiguous(m.group(0))
        out.append(Mention(bag * (n or 1), bag, n is not None, m.start(), m.end()))
    return out


# "5lbs (packed in separate 1lb bags)": the bags a total comes in, not another size.
_PACKED_AS = re.compile(r"\(\s*(?:packed\s+)?(?:in|as)\s+(?:separate|individual)\b", re.I)


def _one(ms: Sequence[Mention]) -> int | None:
    """The weight several statements agree on, or None. A counted statement ("Variety 3 Pack - 12oz A - 12oz B")
    settles it when every other statement is one of its bags."""
    if not ms:
        return None
    counted = [m for m in ms if m.counted]
    if counted:
        first = counted[0]
        if any(not _close(m.grams, first.grams) for m in counted) or any(
            not (_close(m.grams, first.grams) or _close(m.grams, first.bag)) for m in ms if not m.counted
        ):
            return None
        value = first.grams
    else:
        value = ms[0].grams
        if any(not _close(m.grams, value) for m in ms):
            return None
    g = round(value)
    return g if MIN_G <= g <= MAX_G else None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= AGREE * max(a, b)


class Reading(NamedTuple):
    grams: int | None
    # The text says the price buys something other than one weight of coffee with no total given (a pack without a
    # count, a bundle, pods, a drink, a weight that reads two ways): no weight from elsewhere fills it either.
    blocked: bool


def read(text: str | None, where: Literal["option", "title", "tags", "handle"] = "title") -> Reading:
    """The one weight a title, an option, a tag or a URL handle states."""
    text = (text or "").replace("-", " ") if where == "handle" else (text or "")
    if not text.strip():
        return Reading(None, False)
    try:
        ms = mentions(text)
    except _Ambiguous:
        return Reading(None, True)
    stops = (
        [_DRINK]
        + ([_DRINK_NAMES] if where in ("option", "title") else [])
        + ([_NOT_BEANS] if where != "option" else [])
        + ([_TITLE_ONLY] if where == "title" else [])
    )
    if any(p.search(text) for p in stops):
        return Reading(None, True)
    if _PACK.search(text) and not any(m.counted for m in ms):
        return Reading(None, True)
    if _PACKED_AS.search(text) and ms:
        ms = ms[:1]
    g = _one(ms)
    return Reading(g, g is None and bool(ms))


def text_grams(text: str, beans_only: bool = True) -> int | None:
    """The one bag weight a title (beans_only) or an option (not beans_only) states, else None."""
    return read(text, "title" if beans_only else "option").grams


def body_grams(text: str) -> int | None:
    """The one bag weight a description states. Weights about brewing are ignored; a description that offers
    several sizes, a lot, a count of bags or pods gives none."""
    if not text:
        return None
    text = _norm(text)
    try:
        ms = mentions(text)
    except _Ambiguous:
        return None
    kept = []
    for m in ms:
        # the mention's own line, up to 40 characters either side
        near = text[
            max(0, m.start - 40, text.rfind("\n", 0, m.start) + 1) : min(m.end + 40, _line_end(text, m.end))
        ]
        if m.counted or any(p.search(near) for p in (_PACK, _NOT_BEANS, _DRINK, _LOT)):
            return None
        if _SEVERAL_SIZES.search(near) and not _ONLY.search(near):
            return None
        # brewing words right around the mention: "per 12oz of liquid", "20 g of coffee with 320 g of water"
        close_by = text[
            max(0, m.start - 25, text.rfind("\n", 0, m.start) + 1) : min(m.end + 15, _line_end(text, m.end))
        ]
        if _BREWING.search(_WATER_PROCESS.sub(" ", close_by)):
            continue
        kept.append(m)
    return _one(kept)


def tags_grams(tags: Sequence[str] | None) -> int | None:
    """The one size the tags name: every short tag that states a weight must state the same one."""
    values = []
    for t in tags or []:
        if len(t or "") > _TAG_MAX:
            continue
        r = read(t, "tags")
        if r.blocked:
            return None
        if r.grams is not None:
            values.append(r.grams)
    if not values or any(not _close(v, values[0]) for v in values):
        return None
    return values[0]


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j


def _standard(g: int) -> bool:
    return any(_close(g, s) for s in _STANDARD)


def product_grams(
    title: str | None,
    body_text: str | None,
    variants: Sequence[tuple[str | None, float | None, int | None]],
    tags: Sequence[str] | None = None,
    handle: str | None = None,
    unsupported: Unsupported = "drop",
    sources: Sequence[str] = ("title", "tags", "body", "handle"),
) -> list[int | None]:
    """Each variant's grams, from (option title, price, stored grams) and the product's title, tags, description
    and URL handle.

    1. Every option's title is read again with these rules. A weight it states wins (the stored one is kept when
       they agree to rounding). A title that reads as a pack without a count, a drink or two ways blanks the option.
       A stored weight that came from the option's title (models.parse_grams) but doesn't survive these rules is
       dropped ("1/2 lb" stored as 907 g is read as 227; "12oz - 3 Pack" as 1020).
    2. Options still without a weight, when none has one of its own and they cost the same (within SAME_PRICE): the
       one weight the product title states, else the tags' one size, else the description's, else the URL handle's
       (only when the title states no weight). Nothing fills an option step 1 blanked on purpose, or a product
       whose title, tags or description read as a pack, pods or a drink.
    3. A stored weight from elsewhere (Shopify's variant grams): the weight step 2 finds instead; else kept when the
       listing states that weight anywhere (title, description, tags, another option); else, by `unsupported`,
       dropped ("drop", the measured choice: such weights are often shipping weights), kept when it is a standard
       bag size ("standard"), or always kept ("keep"). eval/bag_grams compares them.
    `sources` is the step-2 order; eval/bag_grams compares subsets of it."""
    out: list[int | None] = []
    blocked = False
    elsewhere: set[int] = set()
    for i, (vt, _, stored) in enumerate(variants):
        r = read(vt, "option")
        if r.grams is not None:
            out.append(stored if stored is not None and _close(r.grams, stored) else r.grams)
        elif r.blocked:
            out.append(None)
            blocked = True
        elif stored is not None and parse_grams(vt or "") == stored:
            out.append(None)  # read from this title before, but not by these rules: dropped, not refilled
            blocked = True
        else:
            out.append(stored)
            if stored is not None:
                elsewhere.add(i)
    if not variants:
        return out
    own = [i for i, g in enumerate(out) if g is not None and i not in elsewhere]
    need = [i for i, g in enumerate(out) if g is None or i in elsewhere]
    fill = None
    if need and not own and not blocked:
        prices = [p for p in (variants[i][1] for i in need) if p is not None and p > 0]
        if prices and max(prices) <= SAME_PRICE * min(prices):
            fill = _product_weight(title, body_text, tags, handle, sources)
    stated = None
    for i in need:
        if fill is not None:
            out[i] = fill
        elif i in elsewhere:
            g = out[i]
            if stated is None:
                stated = _stated(title, body_text, tags, [v[0] for v in variants])
            supported = g is not None and any(_close(g, x) for x in stated)
            keep = (
                supported
                or unsupported == "keep"
                or (unsupported == "standard" and g is not None and _standard(g))
            )
            can = _CAN.search(variants[i][0] or "") or _CAN.search(title or "")
            out[i] = g if keep and not blocked and not (can and not supported) else None
    return out


def _stated(
    title: str | None, body: str | None, tags: Sequence[str] | None, options: Sequence[str | None]
) -> list[float]:
    """Every weight the listing states anywhere (one bag each), so a stored weight it states is supported."""
    out: list[float] = []
    for text in [title, body, *(tags or []), *options]:
        try:
            out += [m.bag for m in mentions(text or "")]
        except _Ambiguous:
            continue
    return out


def _product_weight(
    title: str | None,
    body_text: str | None,
    tags: Sequence[str] | None,
    handle: str | None,
    sources: Sequence[str],
) -> int | None:
    """The one weight the product as a whole states, from the first source in `sources` that states one; None when a
    source reads as a pack, pods or a drink."""
    t = read(title, "title")
    if t.blocked:
        return None
    if tags and any(
        _NOT_BEANS.search(x or "") or _DRINK.search(x or "") for x in tags if len(x or "") <= _TAG_MAX
    ):
        return None
    title_states = bool(t.grams) or _has_weight(title)
    for s in sources:
        if s == "title" and t.grams is not None:
            return t.grams
        if s == "tags" and not title_states and (g := tags_grams(tags)) is not None:
            return g
        if s == "body" and not title_states and (g := body_grams(body_text or "")) is not None:
            return g
        if s == "handle" and not title_states and _handle_is_title(handle, title):
            h = read(handle, "handle")
            if h.blocked:
                return None
            if h.grams is not None:
                return h.grams
    return None


_WORD = re.compile(r"[a-z]+")
_HANDLE_NOISE = {
    "oz",
    "ounce",
    "ounces",
    "lb",
    "lbs",
    "pound",
    "pounds",
    "g",
    "kg",
    "bag",
    "bags",
    "coffee",
    "the",
}


def _handle_is_title(handle: str | None, title: str | None) -> bool:
    """A URL handle made from this product's own title: stores copy pages, so a handle naming another coffee
    ("italian-roast-12oz" on "French Roast", "copy-of-...") is not read. Most of its words must be the title's."""
    words = [w for w in _WORD.findall((handle or "").lower()) if w not in _HANDLE_NOISE]
    if not words or "copy" in words:
        return False
    title_words = set(_WORD.findall((title or "").lower()))
    return sum(w in title_words for w in words) >= 0.8 * len(words)


def _has_weight(text: str | None) -> bool:
    try:
        return bool(mentions(text or ""))
    except _Ambiguous:
        return True
