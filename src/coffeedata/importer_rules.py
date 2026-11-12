"""The vocabulary of the importer rules, for the private dbt project that derives each lot's values (dbt_importers/).

The rules themselves are SQL models there (int_importer_rules: process, varieties, altitude, harvest months, grower,
certifications; int_importer_name_rules: what an InterAmerican product name states), tested by dbt unit tests. Their
words and patterns live here and in the catalog's own vocabularies (extract.py, origins.py), so an importer lot and a
roaster listing are described in the same terms; `vocabulary()` hands them all to dbt as one table
(stg_importer_vocab.py), the way stg_products.py calls the catalog's rules. Nothing derived is stored in the
importers dataset: every build re-derives every stored week, so a better rule applies to all of them.

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

# --- Royal Coffee (a Shopify product listing; importers.royal_cells): its title, tags and body text ------------------
# Built only from invented products in Shopify's documented /products.json shape
# (tests/fixtures/importers/royal_synthetic_products.json): nothing here was seen on the store's site. Region,
# varieties, altitude and harvest read only labelled text ("Region: Huila", "Altitude: 1,800 masl"); lot number and
# warehouse read a "Lot 12345" or "<Name> Warehouse" part of the title; origin, process and certifications also read
# the title and the tags that are one word or name (int_royal_rules).

ROYAL_RULES_VERSION = "royal-3"

# "Label: value" in a tag or at the start of a body line (or of a sentence in one: "... lot. Certifications: Organic").
ROYAL_LABELS = {
    "process": r"process(?:ing)?(?: method)?",
    "region": r"region",
    "origin": r"origin|country",
    "variety": r"variet(?:y|ies|als?)|cultivars?",
    "altitude": r"altitude|elevation|growing altitude",
    "harvest": r"harvest(?: season| period| months?)?",
    "certification": r"certifications?|certified",
    "warehouse": r"warehouse",  # never "Location:", which on a coffee listing is usually the farm
}
# Where a line holds another label: after a full stop or semicolon, before a capitalized word and a colon. Case
# matters.
ROYAL_PART_BREAK = r"([.;])\s+([A-Z][a-z]+:)"
ROYAL_TITLE_PARTS = r"\s+[-–|/]\s+|\s*\|\s*"
ROYAL_LOT = r"\blot\s*(?:no\.?|number|#)?\s*#?\s*(\d{3,7}(?:-\d{1,3})?)\b"
ROYAL_YEAR = r"^(?:19|20)\d\d$"  # "Harvest Lot 2025": a year, not a lot number
ROYAL_WAREHOUSE = (
    r"^(?:(?:in|at|ex|stored at)\s+)?([A-Z][\pL\pN_.' ]{1,40}?)\s+warehouse$"
    r"|^warehouse\s*:?\s+([A-Z][\pL\pN_.' ]{1,40})$"
)
# A warehouse "name" that is only a preposition or a negation ("In Warehouse", "Not In Warehouse"): no name at all.
ROYAL_NOT_A_NAME = r"^(?:(?:not|no|out of)\s+)?(?:in|at|ex|our|the|a|stored at|from)?$"
ROYAL_NEGATED = r"\b(?:not|non|without|out of)\b|\bno(?:[^.\pL\pN_]|$)"  # "No. 2 Warehouse" is a number
# Island and state names (origins.ALIASES) that mean one origin in a coffee title; the rest (Java, Flores, Bali,
# Congo) are also a variety, a farm name or two countries, so on a title alone they abstain.
ROYAL_TITLE_PLACES_OK = ("sumatra", "sulawesi", "kona", "hawaii", "puerto rico", "east timor")
ROYAL_DECAF = (
    r"\bdecaf|\bdecaffeinated\b|\bswiss water\b|\bmountain water\b|\bsugar ?cane\b|\bethyl acetate\b|\bco2\b"
)
ROYAL_HONEY = r"\b(honey|pulped natural|semi-?washed|semi washed|wet-?hulled|giling basah)\b"
ROYAL_HARVEST = rf"^\s*{_MONTH}(?:\s*(?:-|–|to|through|until)\s*{_MONTH})?\s*(?:\d{{4}}(?:/\d{{2,4}})?)?\s*$"
_ROYAL_NUM = r"(\d{1,2},\d{3}|\d{3,4})"
ROYAL_ALTITUDE = (
    rf"^\s*{_ROYAL_NUM}(?:\s*(?:-|–|to)\s*{_ROYAL_NUM})?\s*(?:m|masl|m\.a\.s\.l\.?|meters|metres)"
    r"(?:\s+above sea level)?\.?\s*$"
)
ROYAL_CERTS = (
    ("organic", r"\b(?:usda |certified )?organic\b"),
    ("fair_trade", r"\bfair ?trade(?: usa| certified)?\b"),
    ("rainforest_alliance", r"\brainforest alliance(?: certified)?\b|\bRFA\b"),
)
# "FTO" alone can mean Fair Trade Organic or only Fair Trade: it counts only alongside both spelled out. Case matters.
ROYAL_FTO = r"\bFTO\b"
# A modeled certification negated ("Non-Organic", "not Fair Trade"): the claim can't be read, so abstain.
ROYAL_CERT_NEGATED = (
    r"\b(?:non|not|no|without)[\s-]+(?:certified\s+)?(?:usda\s+)?(?:organic|fair ?trade|rainforest)"
)
# What may sit beside the names in a "Variety:" or "Certification:" label; anything else is a name we don't know.
ROYAL_VARIETY_FILLER = r"[\s,;&/+]|\band\b"
ROYAL_CERT_FILLER = r"[\s,;&/+.]|\band\b|\bcertified\b"

# --- an InterAmerican product name: "Brazil Oberon 17/18 Natural", "Guatemala Huehuetenango ASDECAFE SHB EP Organic"

BLEND = r"\bblend\b"
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
    - royal_label: a Royal label's name and its "Label: value" line pattern (group 1 is the value);
    - royal_title_place_ok: an alias that means one origin in a Royal title;
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
        "royal_part_break": ROYAL_PART_BREAK,
        "royal_title_parts": ROYAL_TITLE_PARTS,
        "royal_lot": ROYAL_LOT,
        "royal_year": ROYAL_YEAR,
        "royal_warehouse": ROYAL_WAREHOUSE,
        "royal_not_a_name": ROYAL_NOT_A_NAME,
        "royal_negated": ROYAL_NEGATED,
        "royal_decaf": ROYAL_DECAF,
        "royal_honey": ROYAL_HONEY,
        "royal_harvest": ROYAL_HARVEST,
        "royal_altitude": ROYAL_ALTITUDE,
        "royal_fto": ROYAL_FTO,
        "royal_cert_negated": ROYAL_CERT_NEGATED,
        "royal_variety_filler": ROYAL_VARIETY_FILLER,
        "royal_cert_filler": ROYAL_CERT_FILLER,
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
    rows += [
        {"kind": "royal_label", "term": k, "value": rf"^(?:{v})\s*[:：]\s*(.+?)\s*$"}
        for k, v in ROYAL_LABELS.items()
    ]
    rows += [{"kind": "royal_title_place_ok", "term": t, "value": t} for t in ROYAL_TITLE_PLACES_OK]
    rows += [
        {"kind": "royal_certification", "term": k, "value": v, "ord": i}
        for i, (k, v) in enumerate(ROYAL_CERTS)
    ]
    rows.append({"kind": "rules_version", "term": "rules_version", "value": RULES_VERSION})
    rows.append({"kind": "rules_version", "term": "royal_rules_version", "value": ROYAL_RULES_VERSION})
    return [{"value2": None, "ord": None} | r for r in rows]
