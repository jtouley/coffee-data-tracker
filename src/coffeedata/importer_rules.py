"""The vocabulary of the importer rules, for the private dbt project that derives each lot's values (dbt_importers/).

The rules themselves are SQL models there (int_importer_rules: process, varieties, altitude, harvest months, grower,
certifications; int_importer_name_rules: what an InterAmerican product name states), tested by dbt unit tests. Their
words and patterns live here and in the catalog's own vocabularies (extract.py, origins.py), so an importer lot and a
roaster listing are described in the same terms; `vocabulary()` hands them all to dbt as one table
(stg_importer_vocab.py), the way stg_products.py calls the catalog's rules. Every build re-derives every stored
week from raw, so a better rule applies to all of them; the importers dataset keeps only the latest build's copy
(importers.DERIVED), which nothing reads back.

Patterns are RE2 (DuckDB's regex engine): no lookarounds or backreferences. The rules match them case-insensitively,
except GRADE, which says where case matters.

These rules' quality is "how often is it right", so before anything relies on them they get a CLAUDE.md evaluation
(held-out split, double labels, Wilson intervals). None has been labeled yet: the unit tests show where each rule
decides and where it abstains, not how often it is right.
"""

from __future__ import annotations

import re

from .extract import _PROCESS, _VARIETAL, _VARIETAL_NAME
from .origins import ALIASES, COUNTRIES, REGIONS, canonical_country

RULES_VERSION = "3"

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# A month's name or its three-letter abbreviation, as a whole word: "Dec" and "December", never "declined".
_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?"
)
HARVEST = (
    rf"\bharvest(?:ed)?\b\s*(?:months?|period|season)?\s*[:\-–]?\s*(?:in\s+|from\s+)?{_MONTH}"
    rf"(?:\s*(?:-|–|to|through|until)\s*{_MONTH})?"
)

# Altitude only where the text says it is one: a unit that means above sea level, or an altitude word before it.
_NUM = r"(\d{1,2}[,.]?\d{3}|\d{3,4})"
ALTITUDE = (
    rf"\b(?:altitudes?|elevations?|grown at|alt)\b\.?[^0-9\n]{{0,20}}{_NUM}"
    rf"(?:\s*(?:-|–|to)\s*{_NUM})?\s*(?:m\b|masl\b|m\.a\.s\.l\.?|meters\b|metres\b)?"
    rf"|{_NUM}(?:\s*(?:-|–|to)\s*{_NUM})?\s*(?:masl\b|m\.a\.s\.l\.?|meters above sea level|metres above sea level)"
)

DECAF = r"\bdecaf|\bdecaffeinated\b|\bswiss water\b|\bmountain water\b|\bsugar ?cane\b|(?-i:\bEA\b)"
# Honey and its relatives, as the catalog's model counts them (extract._PROCESS); matched before natural and washed,
# whose words they contain ("pulped natural", "semi-washed").
HONEY = r"\b(honey|pulped natural|semi-washed|semi washed|wet-hulled|wet hulled|giling basah)\b"

GROWER_TYPES = (
    ("micromill", r"\bmicro-?mill\b"),
    ("washing_station", r"\bwashing station\b|\bstation\b"),
    ("cooperative", r"\bco-?operativa?\b|\bco-?op\b|\basociaci[oó]n\b|\bassociation\b"),
    ("farm", r"\bfinca\b|\bfazendas?\b|\bhacienda\b|\bestate\b|\bfarm\b|\bs[ií]tio\b"),
)
# Packaging or a placeholder, not part of a grower's name
PACKAGING = r"\s*\((?:grainpro|sc bags|jute[^)]*|ecotact|vacuum[^)]*)\)\s*$"
PLACEHOLDER = r"\bTBC\b|\bTBD\b"

# The certifications we model: (name, claimed, marked absent on the page).
CERTIFICATIONS = (
    ("organic", r"\borganic\b", r"\bnorg\b"),
    ("fair_trade", r"\bfair ?trade\b|\bFTO\b", r"\bnft\b"),
    ("rainforest_alliance", r"\brainforest\b|\bRFA\b", None),
)
# Certifications and programs we don't model: naming one makes the whole list unknown, never "none". SMBC is the
# Smithsonian Migratory Bird Center's Bird Friendly seal, as InterAmerican abbreviates it.
OTHER_CERTS = (
    r"\bbird[- ]friendly\b|\bsmithsonian\b|\bsmbc\b|\bc\.?a\.?f\.?e\.? practices\b|\butz\b|\b4c\b|\bregenerative\b|"
    r"\bdemeter\b|\bbiodynamic\b|\bkosher\b"
)

# --- Royal Coffee (a Shopify product listing; importers.royal_cells): its title, tags, product type and variants ----
# Royal's titles are " - "-separated parts: the coffee's name, which leads with its origin ("COLOMBIA FT-FLO/USA HUILA",
# "Decaf Peru Organic ..."), then any of a contract or ICO number ("*12345*"), Royal's own lot ID (five digits, with a
# split suffix: "12345", "12345-2"), the bags ("69.0 kg GrainPro Bags"), a shipment or arrival date ("May 2027
# Shipment", "ETA: Nov 30, 2026"), and last the warehouse, in capitals, after "SPOT" when the lot is there now
# ("SPOT EXAMPLEWHSE"). The body is prose (no "Label: value" lines), so region, varieties, altitude and harvest are
# left for Claude. The rules (int_royal_rules) were written against the real listing's shapes; every example here and
# in the tests is invented.

ROYAL_RULES_VERSION = "royal-4"

ROYAL_TITLE_PARTS = r"\s+[-–]\s+"
# A part that is exactly Royal's lot ID: five digits, and a split lot's suffix. Never a producer's "Lot 3", a starred
# contract number or a year.
ROYAL_LOT_ID = r"^\d{5}(?:-\d{1,2})?$"
# The last part, when it is a warehouse code in capitals, optionally after SPOT (the lot is in that warehouse now) and
# with a bay suffix ("SPOT EXAMPLEWHSE B2"). Case matters: a name part is never all capitals and digits like this.
ROYAL_WAREHOUSE = r"(?-i)^(SPOT\s+)?([A-Z][A-Z0-9]{3,}(?:\s+[A-Z][0-9])?)$"
# When a lot that isn't in a warehouse yet is due: an "ETA: Nov 30, 2026" part (group 1), or a "January 2027 Delivery"
# part (group 2). A "... Shipment" or "Intend Ship: ..." part is when it leaves origin, not when it arrives, so it
# says nothing about arrival.
ROYAL_ARRIVAL = r"^(?:eta\s*:\s*(\pL{3,9}\.?\s+\d{1,2},\s*\d{4})|(\pL{3,9}\s+\d{4})\s+delivery)$"
# A bag weight the title states: "69.0 kg Bags", "1000.0 kg Super Sack", "22 Lb" (group 1 the number, 2 the unit).
ROYAL_BAG_WEIGHT = r"\b(\d+(?:\.\d+)?)\s*(kg|lb)s?\b"
# The title part that names the packaging, as written: "69.0 kg GrainPro Bags", "Ecotact Bags", "20.0 kg Boxes".
ROYAL_BAG_PART = r"\b(?:bags?|box(?:es)?|super ?sacks?)$"
# A tag that is a process and nothing else ("washed", "honey-processed", "wet-hulled", "decaf"): the process as stated.
ROYAL_PROCESS_TAG = (
    r"^(?:washed|natural|honey|anaerobic|decaf|wet-?hulled|semi-?washed|pulped-?natural)(?:-?processed)?$"
)
# Places a Royal title can lead with that the catalog's lists (origins.COUNTRIES, ALIASES) don't spell this way, and
# their country. Bare "Timor" is Timor-Leste in a coffee name (West Timor's coffee is sold as Indonesian).
ROYAL_PLACES = {"timor leste": "Timor-Leste", "timor": "Timor-Leste", "komodo": "Indonesia"}
# A first place that names no one country: the island two countries share, and "Congo" (two countries).
ROYAL_AMBIGUOUS_PLACES = ("new guinea", "congo")
ROYAL_DECAF = (
    r"\bdecaf|\bdecaffeinated\b|\bswiss water\b|\bmountain water\b|\bsugar ?cane\b|\bethyl acetate\b|\bco2\b"
)
ROYAL_HONEY = r"\b(honey|pulped natural|semi-?washed|semi washed|wet-?hulled|giling basah)\b"
ROYAL_CERTS = (
    ("organic", r"\b(?:usda |certified )?organic\b"),
    # Royal writes fair trade as FT, FT-FLO, FT-USA or FT-FLO/USA (case matters: "ft" is also feet)
    ("fair_trade", r"\bfair ?trade(?: usa| certified)?\b|(?-i:\bFT\b)"),
    ("rainforest_alliance", r"\brainforest alliance(?: certified)?\b|\bRFA\b"),
)
# "FTO" alone can mean Fair Trade Organic or only Fair Trade: it counts only alongside both spelled out. Case matters.
ROYAL_FTO = r"\bFTO\b"
# A modeled certification negated ("Non-Organic", "not Fair Trade"): the claim can't be read, so abstain.
ROYAL_CERT_NEGATED = (
    r"\b(?:non|not|no|without)[\s-]+(?:certified\s+)?(?:usda\s+)?(?:organic|fair ?trade|rainforest)"
)


def _royal_origin_lead() -> str:
    """The first place a Royal title's name part names, the longest name first ("Papua New Guinea" before "New
    Guinea" and "Guinea", "Timor Leste" before "Timor"), as group 1."""
    return rf"\b({_alternation([*COUNTRIES, *ROYAL_PLACES])}){_WORD_END}"


def catalog_countries() -> list[str]:
    """The countries the catalog's origins resolve to (origins.canonical_country over COUNTRIES), without the
    island two countries share ("New Guinea", which canonical_country leaves as it is)."""
    return sorted({canonical_country(c) for c in COUNTRIES} - {"New Guinea"})


# --- an InterAmerican product name: "Brazil Oberon 17/18 Natural", "Guatemala Huehuetenango ASDECAFE SHB EP Organic"

BLEND = r"\bblend\b"
# Island and state names (origins.ALIASES) that mean one origin in a product name; the rest (Java, Flores, Bali, Congo)
# are also a variety, a farm name or two countries, so they never count as an origin there.
TITLE_PLACES_OK = ("sumatra", "sulawesi", "kona", "hawaii", "puerto rico", "east timor", "dr congo")
# A farm's name ("Finca Santa Barbara") says nothing about where the farm is (as extract._FARM_NAME).
FARM_WORD = r"\b(?:finca|hacienda|fazenda|s[ií]tio|beneficio|estate|farm)\s+"
# The grade and preparation words of the coffee trade, as importers write them. Case matters for the abbreviations
# ("AA" and "EP" are grades, "aa" or "ep" in a word are not); "Grade 2", "EP 10" and the words don't. A bare letter
# ("Papua New Guinea B") is left out: it could be anything.
GRADE = r"\b(G[1-5]|(?i:grade) [1-5]|UG|SHB|SHG|HG|TP|AA|AB|PB|EP ?10|EP|(?i:supremo|excelso|peaberry))\b"
# Numbers joined by slashes ("17/18", "14/16", "11/12/2026"): a screen size range when the name has exactly one and it
# is two sizes, the smaller first, both 12-20 and at most 4 apart (checked by the rule).
SCREEN = r"\d+(?:/\d+)+"


def _alternation(words: list[str]) -> str:
    """Whole words, the longest first, so "Papua New Guinea" is tried before "Guinea"."""
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


# RE2's \b is ASCII-only: "Catuaí " has no \b after the "í". A name list ends instead at a character that isn't a
# letter, digit or underscore in any script, kept in group 2 so a replacement can put it back ("\2").
_WORD_END = r"([^\pL\pN_]|$)"


# Whitespace as Python's \s sees it in a str pattern, which RE2's \s (ASCII only) doesn't: also a non-breaking space
# and the other Unicode spaces (the Z categories), U+0085 and U+001C-U+001F. Royal's tags and titles are stored as the
# store wrote them, so "Origin:<NBSP>Colombia" must read as "Origin: Colombia" (dbt_importers' ws() macro is the same).
WS = r"\s\p{Z}\x{0b}\x{85}\x{1c}-\x{1f}"


def re2(pattern: str) -> str:
    r"""A Python pattern with its \s and \S meaning what they mean in Python (Unicode whitespace), for RE2."""
    out, in_class, i = [], False, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt == "s":
                out.append(WS if in_class else f"[{WS}]")
            elif nxt == "S":
                assert not in_class, pattern
                out.append(f"[^{WS}]")
            else:
                out.append(c + nxt)
            i += 2
            continue
        if c == "[" and not in_class:
            in_class = True
        elif c == "]" and in_class:
            in_class = False
        out.append(c)
        i += 1
    return "".join(out)


def _words(alternation: str) -> str:
    return rf"\b({alternation}){_WORD_END}"


def _varietal() -> str:
    """The catalog's variety pattern (extract._VARIETAL), with a word end that works in RE2."""
    p = _VARIETAL.pattern
    assert p.startswith(r"\b(") and p.endswith(r")\b"), p
    return _words(p[3:-3])


def vocabulary() -> list[dict]:
    """Every word and pattern the importer rules use, as rows (kind, term, value, value2, ord):
    - pattern: a named RE2 pattern (term) in value;
    - process: the catalog's process types in order (term) and their patterns (extract._PROCESS);
    - grower_type: a grower type and its pattern, in the order they are tried;
    - certification: a modeled certification, its claimed pattern and (value2) its marked-absent one;
    - varietal: a variety's spelling key (lowercase, no spaces or hyphens) and its catalog spelling;
    - country: a country or alias (lowercase) and the catalog's country for it;
    - region: a one-country growing region (lowercase) and its country (origins.REGIONS);
    - country_name: a name in origins.COUNTRIES as written, its country, and (value2) "alias" for an island or state;
    - royal_place: a place a Royal title can lead with that the catalog spells otherwise, and its country;
    - royal_ambiguous_place: a first place in a Royal title that names no one country;
    - catalog_country: a country the catalog's origins resolve to (catalog_countries()), the only values an origin
      may take;
    - title_place_ok: an alias that means one origin in an InterAmerican name;
    - royal_certification: a certification and its pattern in Royal's wording;
    - rules_version: RULES_VERSION (term rules_version) and ROYAL_RULES_VERSION (term royal_rules_version)."""
    patterns = {
        "harvest": HARVEST,
        "altitude": ALTITUDE,
        "decaf": DECAF,
        "honey": HONEY,
        "packaging": PACKAGING,
        "placeholder": PLACEHOLDER,
        "other_certs": OTHER_CERTS,
        "varietal": _varietal(),
        "blend": BLEND,
        "farm_word": FARM_WORD,
        "grade": GRADE,
        "screen": SCREEN,
        # the name leads with a country or alias; any country or alias anywhere; any one-country region
        "country_lead": rf"^\s*({_alternation(COUNTRIES)}){_WORD_END}",
        "country": _words(_alternation(COUNTRIES)),
        "region": _words(_alternation(list(REGIONS))),
        # Royal Coffee
        "royal_title_parts": ROYAL_TITLE_PARTS,
        "royal_lot_id": ROYAL_LOT_ID,
        "royal_warehouse": ROYAL_WAREHOUSE,
        "royal_arrival": ROYAL_ARRIVAL,
        "royal_bag_weight": ROYAL_BAG_WEIGHT,
        "royal_bag_part": ROYAL_BAG_PART,
        "royal_process_tag": ROYAL_PROCESS_TAG,
        "royal_origin_lead": _royal_origin_lead(),
        "royal_decaf": ROYAL_DECAF,
        "royal_honey": ROYAL_HONEY,
        "royal_fto": ROYAL_FTO,
        "royal_cert_negated": ROYAL_CERT_NEGATED,
    }
    rows: list[dict[str, str | int | None]] = [
        {"kind": "pattern", "term": k, "value": v} for k, v in patterns.items()
    ]
    rows += [{"kind": "process", "term": k, "value": v, "ord": i} for i, (k, v) in enumerate(_PROCESS)]
    rows += [
        {"kind": "grower_type", "term": k, "value": v, "ord": i} for i, (k, v) in enumerate(GROWER_TYPES)
    ]
    rows += [
        {"kind": "certification", "term": k, "value": yes, "value2": no, "ord": i}
        for i, (k, yes, no) in enumerate(CERTIFICATIONS)
    ]
    rows += [{"kind": "varietal", "term": k, "value": v} for k, v in _VARIETAL_NAME.items()]
    countries = {c.lower(): c for c in COUNTRIES} | ALIASES  # an alias names the country it belongs to
    rows += [{"kind": "country", "term": k, "value": v} for k, v in countries.items()]
    rows += [{"kind": "region", "term": k, "value": v} for k, v in REGIONS.items()]
    # the catalog's country names as written, each with its country, and whether it is an alias (an island or state)
    rows += [
        {
            "kind": "country_name",
            "term": c,
            "value": canonical_country(c),
            "value2": "alias" if c.lower() in ALIASES else None,
        }
        for c in COUNTRIES
    ]
    rows += [{"kind": "royal_place", "term": k, "value": v} for k, v in ROYAL_PLACES.items()]
    rows += [{"kind": "royal_ambiguous_place", "term": t, "value": t} for t in ROYAL_AMBIGUOUS_PLACES]
    rows += [{"kind": "catalog_country", "term": c, "value": c} for c in catalog_countries()]
    rows += [{"kind": "title_place_ok", "term": t, "value": t} for t in TITLE_PLACES_OK]
    rows += [
        {"kind": "royal_certification", "term": k, "value": v, "ord": i}
        for i, (k, v) in enumerate(ROYAL_CERTS)
    ]
    rows.append({"kind": "rules_version", "term": "rules_version", "value": RULES_VERSION})
    rows.append({"kind": "rules_version", "term": "royal_rules_version", "value": ROYAL_RULES_VERSION})
    pattern_kinds = {
        "pattern",
        "process",
        "grower_type",
        "certification",
        "royal_certification",
    }
    for r in rows:
        if r["kind"] in pattern_kinds:
            r["value"] = re2(str(r["value"]))
            if r.get("value2"):
                r["value2"] = re2(str(r["value2"]))
    return [{"value2": None, "ord": None} | r for r in rows]
