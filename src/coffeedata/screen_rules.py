"""The roaster screen's rules, shared by the job, dbt and the evaluation (docs/design/roaster-screen.md).

- next_due: when a screened place is due again. The job's selection (screen.select) and dbt's
  `screen_scores.next_due` both call it. It reads only what a screen recorded, never the clock.
- product_signals, home_signals, features and tier: what a screen's products and home page say, and the tier that
  follows (suggested, review, dropped, not_screened). dbt calls them (int_screen_products.py, int_screen_home.py,
  screen_scores.py), and the evaluation calls the same functions (eval/roaster_screen/screen_eval.py).

The tier is PROVISIONAL and shown to nobody yet: eval/roaster_screen/README.md has what was measured and what wasn't.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import bag_weight, classify, extract, products
from .origins import COUNTRIES

# Section 3.5 (the owner's D8). A transient failure says nothing about the site, so it is asked again soon, twice;
# an answer about the site goes stale slowly; a refusal is the site saying no.
RETRY_DAYS = 28
RETRY_TRIES = 2
RESCREEN_DAYS = 180
REFUSED_DAYS = 365

# The site, or its edge, refused us: robots.txt disallows the path or answered 401 or 403, or an edge challenged.
REFUSED = ("robots_disallow", "robots_401_403", "challenge")


# Screens that got no answer about the site through no fault of the site's own: a home page that timed out, a
# request cap spent before the home page answered, or an error in our own code.
NO_ANSWER_STATUSES = ("timeout", "budget_spent", "error")


def transient(status: str | None, refusal: str | None) -> bool:
    """A screen that got no answer about the site and may get one soon: its robots.txt didn't answer, the site
    answered 429, 430 or a 5xx (pause_<code>: one that asked us to wait), or the screen itself got no answer
    (NO_ANSWER_STATUSES)."""
    status = status or ""
    return (
        status == "robots_unavailable"
        or refusal in ("http_429", "http_430")
        or status in ("http_429", "http_430")
        or status.startswith(("http_5", "pause_"))
        or status in NO_ANSWER_STATUSES
    )


def next_due(
    screened_at: datetime, status: str | None, refusal: str | None, tier: str | None = None, tries: int = 1
) -> date:
    """The first day a place screened at `screened_at` may be screened again.

    tries: how many screens in a row, this one included, were transient: the first waits RETRY_DAYS and the
    second RESCREEN_DAYS, so a place is tried twice. tier: the screen's tier, when the caller
    has one. Every answered tier (suggested, review, dropped) shares one window today, so the tier doesn't change
    the answer; it is a parameter so a later window per tier changes this function and nothing else. A place the
    owner has flagged is never due: selection leaves it out before asking."""
    day = screened_at.date()
    if refusal in REFUSED:
        return day + timedelta(days=REFUSED_DAYS)
    if transient(status, refusal):
        return day + timedelta(days=RETRY_DAYS if tries < RETRY_TRIES else RESCREEN_DAYS)
    return day + timedelta(days=RESCREEN_DAYS)


# --- features and the tier (sections 4.1 and 4.2) --------------------------------------------------------------------
#
# Everything below is a pure function of what a screen recorded (stg_screens, stg_screen_products), the place's name
# and the registry's roaster names. dbt calls it (int_screen_products.py, int_screen_home.py, screen_scores.py), and so
# does the evaluation (eval/roaster_screen/screen_eval.py), so the two can't disagree. Nothing here fetches anything.
#
# The thresholds are PROVISIONAL (eval/roaster_screen/README.md): tuned on the train set against #130's stored
# evidence, cut to the first page. They are chosen on validation and scored on a sealed test in the design's PR 5.
# Until then nothing shows a tier to the owner. A change to how tier() decides needs the CLAUDE.md evaluation.

RULE_VERSION = "provisional-1"
TIERS = ("suggested", "review", "dropped", "not_screened")


@dataclass(frozen=True)
class Rule:
    """The suggested tier's thresholds (rule 4). The candidates the evaluation compares differ only here."""

    name: str = "full"
    min_named: int = 5  # N: coffees named as coffee on the first page (#130 chose 5)
    coffee_share: float = 0.2  # s: of the page's products
    bag_share: float = 0.5  # b: of its named coffees, those sold in a bag-sized option
    own_vendor_share: float = 0.5  # its own name on its bags; None: only roastery wording counts
    depth: bool = True  # origins >= MIN_ORIGINS or roast_share >= MIN_ROAST_SHARE
    own_vendor_path: bool = True


RULE = Rule()
MIN_ROAST_SHARE = 0.5
MIN_ORIGINS = 2
MAX_VENDORS = 3  # this many distinct vendors among its coffees: a multi-roaster shop
MAX_WHOLESALE_SHARE = 0.8
BAG_GRAMS = (150, 2500)  # classify._bag_sized
# Reason codes that say the place may resell another roaster's coffee. A suggested place never carries one
# (dbt/tests/screen_scores_suggested_has_no_reseller_code.sql).
RESELLER_CODES = ("other_roaster", "vendors", "serves", "wholesale")
# The catalog APIs phase 1 reads (the owner's D7): a place on another platform is judged from its home page alone.
READ_PLATFORMS = ("shopify", "woocommerce")
DEAD_STATUSES = ("unreachable", "http_404", "http_410")

_GENERIC = {
    "coffee", "coffees", "roast", "roasters", "roaster", "roasting", "roastery", "roasted", "roastworks", "co", "company",
    "cafe", "caffe", "the", "and", "house", "shop", "llc", "inc", "bar", "espresso", "tea", "teas", "kitchen",
    "bakery", "of", "a", "beans", "bean", "brew", "brewing", "café", "lab", "works", "trading", "supply",
}  # fmt: skip

# Wording that the store roasts its own coffee (#130's, tuned on 50,000 characters of home page text).
ROAST_WORDS = re.compile(
    r"\b(we roast|we(?:'re| are) (?:a |an )?(?:small[- ]batch |independent |specialty |local |family[- ]owned )*"
    r"(?:coffee )?roast(?:er|ery)|roasted (?:in[- ]house|on[- ]site|by us|here|right here|in our)|"
    r"(?:our|own) roast(?:ery|ing (?:facility|plant|room))|roast(?:ed|ing)? (?:all )?(?:of )?our own|"
    r"in[- ]house roast(?:ing|ed|ery)?|roast(?:ed|ing) (?:it )?ourselves|we source and roast|sourced and roasted by us|"
    r"micro[- ]?roast(?:er|ery)|small[- ]batch roast(?:er|ery|ed|ing))\b",
    re.I,
)
# Wording that a café serves, or a shop sells, another roaster's coffee: a serving verb followed by a capitalized
# name ("We proudly serve Passenger Coffee"), counted only when the words after it name a roaster we know or say
# coffee, roaster or roastery ("Proudly serving Portland since 2005" and "We serve a full breakfast" don't count).
SERVES_WORDS = re.compile(
    r"(?i:\bproudly (?:serv(?:e|es|ing)|brew(?:s|ing)?|pour(?:s|ing)?)|\bwe (?:serve|brew|pour|carry|feature)|"
    r"\bfeaturing|\bcoffee (?:is )?(?:roasted|supplied) by|\bour (?:coffee )?roasting partners?(?: is| are)?|"
    r"\bpartner roaster(?:y|s)?(?: is| are)?) (?!(?:Our|The|You|Your|A|An|This|Local|Fresh|Great|Delicious|Quality)\b)"
    r"([A-Z][\w'&-]*(?: [A-Z][\w'&-]*){0,3})"
)
_SERVES_COFFEE = re.compile(r"\b(coffee|coffees|roasters?|roastery|roasting|espresso)\b", re.I)


def brand_key(name: str) -> str:
    """A name's distinctive words, lower case, accents and punctuation dropped: "Rival Bros Coffee" -> "rival bros"."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", "")
    return " ".join(w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _GENERIC)


def _squash(s: str) -> str:
    return s.replace(" ", "")


def domain_stem(domain: str) -> str:
    """The domain's name part: "rivalbros.com" -> "rivalbros", "shop.example.co.uk" -> "example"."""
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net"):
        parts = parts[:-1]
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_own(vendor: str, name: str, domain: str) -> bool:
    """A vendor that names the store itself: its brand key, or its domain's name, exactly. Never a substring: "Counter
    Culture Coffee" is not the own brand of a café called "The Counter"."""
    v = _squash(brand_key(vendor))
    return len(v) >= 3 and v in {_squash(brand_key(name)), domain_stem(domain)}


# Words that name a coffee rather than a business, so a known roaster's name made only of them ("Dark Roast", "Single
# Origin", "Honduras Coffee") is never matched in another store's titles.
_COFFEE_VOCAB = {
    "light", "medium", "dark", "french", "italian", "vienna", "city", "full", "blend", "blends", "single", "origin",
    "decaf", "organic", "fair", "trade", "local", "house", "morning", "breakfast", "signature", "classic", "cold",
    "whole", "ground", "fresh", "craft", "specialty", "small", "batch", "premium", "gourmet", "estate", "reserve",
}  # fmt: skip


class KnownRoasters:
    """Brand keys of the roasters we already know (the registry), to tell a vendor or title naming another roaster. A
    vendor must name one whole. In a title only a key of two or more words counts, and not one made only of coffee
    words or origins: single words ("Autumn", "Local", "Origin") are blend names as often as roasters."""

    def __init__(self, names: Iterable[str]):
        vocab = _COFFEE_VOCAB | {w for c in COUNTRIES for w in brand_key(c).split()}
        self.keys = {k for k in (brand_key(n) for n in names) if len(_squash(k)) >= 3}
        title_keys = sorted(
            (k for k in self.keys if len(k.split()) >= 2 and not set(k.split()) <= vocab),
            key=lambda k: (-len(k), k),
        )
        self._title = (
            re.compile(r"\b(" + "|".join(re.escape(k) for k in title_keys) + r")\b") if title_keys else None
        )

    def vendor(self, vendor: str, own: str) -> bool:
        k = brand_key(vendor)
        return bool(k) and k != own and k in self.keys

    def in_title(self, title: str, own: str) -> str | None:
        if self._title is None:
            return None
        for m in self._title.finditer(brand_key(title)):
            if m.group(1) != own and m.group(1) not in own:
                return m.group(1)
        return None


# A coffee named as one in its title, type or tags. On a café's or gift shop's store the rules, tuned on roasters'
# catalogs, also call jams, dessert bars and gift boxes coffee (#130's train set); a roaster's coffees nearly always
# say coffee, espresso, roast, blend, decaf or beans, or name an origin.
_NAMED = re.compile(
    r"\b(coffees?|espresso|roast(?:ed)?|blend|decaf|beans?|whole[- ]bean|single[- ]origin|"
    + "|".join(re.escape(c) for c in COUNTRIES)
    + r")\b",
    re.I,
)


def named_coffee(title: str, product_type: str, tags: Iterable[str]) -> bool:
    return bool(_NAMED.search(" | ".join([title or "", product_type or "", *(tags or [])])))


def product_signals(p: dict, name: str, domain: str, known: KnownRoasters) -> dict:
    """What one product on a screen's page says (int_screen_products). p: a screen_products row (title, vendor,
    product_type, tags, variants, body_hash, body_text, handle). Rules only: no verdict is asked or read."""
    variants = p.get("variants") or []
    options = [(v.get("title"), v.get("price"), v.get("grams")) for v in variants]
    grams = bag_weight.product_grams(
        p.get("title"), p.get("body_text"), options, tags=p.get("tags") or [], handle=p.get("handle")
    )
    item = products.item(
        p.get("title"), p.get("product_type"), p.get("tags"), grams, p.get("body_hash"), p.get("body_text")
    )
    is_coffee, _ = products.decide(item, None)
    named = is_coffee and named_coffee(item.title, item.product_type, item.tags)
    attrs = extract.rules(item) if named else None
    vendor = p.get("vendor") or ""
    own_key = brand_key(name)
    own = is_own(vendor, name, domain)
    other = (not own and known.vendor(vendor, own_key)) or bool(known.in_title(item.title, own_key))
    return {
        "is_coffee": is_coffee,
        "named": named,
        "is_wholesale": bool(is_coffee and classify.is_wholesale(item)),
        "roast_level": attrs.roast_level if attrs else None,
        "origin_country": attrs.origin_country if attrs else None,
        "is_blend": attrs.is_blend if attrs else None,
        "bag_grams": sorted({g for g in grams if g and BAG_GRAMS[0] <= g <= BAG_GRAMS[1]}) if named else [],
        "vendor_key": brand_key(vendor) if named else "",
        "own_vendor": bool(named and own),
        "other_roaster": bool(named and other),
        "currencies": sorted({c.upper() for v in variants if (c := v.get("currency"))}),
    }


def home_signals(home_text: str | None, name: str, known: KnownRoasters) -> dict:
    """Roastery and "proudly serving" wording, from the home page's own text only (int_screen_home): a reseller's
    product descriptions are often the source roaster's words, so product text is never read for this."""
    text = home_text or ""
    own = brand_key(name)
    roast = ROAST_WORDS.search(text)
    serves = None
    for m in SERVES_WORDS.finditer(text):
        who = m.group(1)
        if brand_key(who) == own:
            continue
        if (
            known.in_title(who, own)
            or known.vendor(who, own)
            or _SERVES_COFFEE.search(text[m.end(1) : m.end(1) + 40])
            or _SERVES_COFFEE.search(who)
        ):
            serves = m
            break
    return {
        "roast_phrase": " ".join(roast.group(0).split())[:80] if roast else None,
        "serves_phrase": " ".join(serves.group(0).split())[:80] if serves else None,
    }


def features(screen: dict, product_rows: Iterable[dict], home: dict) -> dict:
    """One screen's features (section 4.1) from its row, its products' signals and its home page's wording. The same
    arithmetic as dbt's int_screen_features.sql, which a test holds to this (tests/test_screen_rules.py)."""
    rows = list(product_rows)
    named = [r for r in rows if r["named"]]
    n = len(named)
    origins = Counter(r["origin_country"] for r in named if r["origin_country"])
    currencies = sorted({c for r in rows for c in r["currencies"]})
    share = lambda k: (k / n) if n else 0.0  # noqa: E731
    return {
        "status": screen.get("status"),
        "refusal": screen.get("refusal"),
        "platform": screen.get("platform") or "",
        "page_full": bool(screen.get("page_full")),
        "page_timeout": bool(screen.get("page_timeout")),
        "products": len(rows),
        "coffees": sum(r["is_coffee"] for r in rows),
        "named": n,
        "coffee_share": n / len(rows) if rows else 0.0,
        "bag_share": share(sum(bool(r["bag_grams"]) for r in named)),
        "bag_sizes": sorted({g for r in named for g in r["bag_grams"]}),
        "roast_share": share(sum(r["roast_level"] is not None for r in named)),
        "origins": len(origins),
        "origin_repeat": sum(c >= 2 for c in origins.values()),
        "own_vendor": sum(r["own_vendor"] for r in named),
        "own_vendor_share": share(sum(r["own_vendor"] for r in named)),
        "other_roaster": sum(r["other_roaster"] for r in named),
        "vendors": len({r["vendor_key"] for r in named if r["vendor_key"]}),
        "wholesale_share": share(sum(r["is_wholesale"] for r in named)),
        "currencies": currencies,
        "roast_phrase": home.get("roast_phrase"),
        "serves_phrase": home.get("serves_phrase"),
    }


# Rule 3's reseller doubts, by the code each reason starts with (reason_code). A place with any of them goes to a
# person and is never sent to Claude (screen_verdict.undecided reads these, never the reason's text).
OTHER_ROASTER, MANY_VENDORS, SERVES, WHOLESALE = "other_roaster", "vendors", "serves", "wholesale"
DOUBT_CODES = frozenset({OTHER_ROASTER, MANY_VENDORS, SERVES, WHOLESALE})


def reason_code(reason: str) -> str:
    """A reason's stable code: the word before its first space or colon ("serves:Proudly serving X" -> serves)."""
    return re.split(r"[ :]", reason, maxsplit=1)[0]


def is_doubt(reasons: Iterable[str]) -> bool:
    """Whether a tier's reasons hold one of rule 3's reseller doubts (DOUBT_CODES)."""
    return any(reason_code(r) in DOUBT_CODES for r in reasons)


def tier(f: dict, rule: Rule = RULE) -> tuple[str, list[str]]:
    """(tier, reasons) for one screen's features: section 4.2's rules, first match wins. The rules abstain rather
    than guess: a refusal or an unread platform is not_screened, a doubt always goes to a person (review), and
    dropped is only for a dead site, a whole catalog with no coffee, or a store that doesn't price in US dollars."""
    status, refusal = f.get("status") or "", f.get("refusal")
    roast, serves = f.get("roast_phrase"), f.get("serves_phrase")
    read = f["platform"] in READ_PLATFORMS
    # 1 and 2: no answer, or a dead site
    if status in DEAD_STATUSES:
        return "dropped", [f"dead:{status}"]
    if status != "ok":
        return "not_screened", [f"refused:{refusal}" if refusal else f"no_answer:{status}"]
    if f["page_timeout"]:
        return "review", ["page_timeout"]
    if not read:
        if roast:
            return "review", [f"unread_platform:{f['platform']}", "roast_words"]
        return "not_screened", [f"unread_platform:{f['platform']}"]
    foreign = [c for c in f["currencies"] if c != "USD"]
    if foreign and "USD" not in f["currencies"]:
        return "dropped", [f"currency:{','.join(foreign)}"]
    if f["products"] >= 1 and f["named"] == 0 and not roast and not f["page_full"]:
        return "dropped", [f"no_coffee 0/{f['products']}"]
    if f["products"] == 0 and not roast:
        return "dropped", ["empty_catalog"]
    if f["named"] == 0:  # a full first page with no coffee, or roastery wording and none for sale on it
        return "review", [f"no_coffee 0/{f['products']}"] + (["page_full"] if f["page_full"] else []) + (
            ["roast_words"] if roast else []
        )
    # 3: reseller doubt
    doubt = []
    if f["other_roaster"] >= 1:
        doubt.append(f"{OTHER_ROASTER} {f['other_roaster']}/{f['named']}")
    if f["vendors"] >= MAX_VENDORS:
        doubt.append(f"{MANY_VENDORS} {f['vendors']}")
    if serves and not roast:
        doubt.append(f"{SERVES}:{serves}")
    if f["wholesale_share"] >= MAX_WHOLESALE_SHARE:
        doubt.append(f"{WHOLESALE} {f['wholesale_share']:.0%}")
    evidence = [f"named {f['named']}/{f['products']}"]
    if f["own_vendor"]:
        evidence.append(f"own_vendor {f['own_vendor']}/{f['named']}")
    if roast:
        evidence.append("roast_words")
    if f["origins"]:
        evidence.append(f"origins {f['origins']}")
    if f["bag_sizes"]:
        evidence.append("bags " + ",".join(f"{g}g" for g in f["bag_sizes"][:6]))
    if doubt:
        return "review", doubt + evidence
    # 4: confidently a roaster selling online
    own = rule.own_vendor_path and f["own_vendor_share"] >= rule.own_vendor_share
    roaster = own or bool(roast)
    depth = not rule.depth or f["origins"] >= MIN_ORIGINS or f["roast_share"] >= MIN_ROAST_SHARE
    if (
        f["named"] >= rule.min_named
        and f["coffee_share"] >= rule.coffee_share
        and f["bag_share"] >= rule.bag_share
        and roaster
        and depth
    ):
        return "suggested", evidence
    # 5: everything else with at least one coffee
    return "review", evidence
