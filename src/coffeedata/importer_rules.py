"""Our model's values for an importer's lot, derived from its raw cells (importer_raw) whenever they are read.

Nothing here is stored: importers.offers() applies these rules to the whole raw history at read time, so a better
rule re-derives every week. Each rule is deterministic and abstains (None) when the text doesn't settle it; a value
we can't tell is left empty, never guessed. Process, varieties and the altitude range use the catalog's own
vocabularies (extract.py), so an importer lot and a roaster listing are described in the same terms.

These rules' quality is "how often is it right", so before anything relies on them they get a CLAUDE.md evaluation
(held-out split, double labels, Wilson intervals). None has been labeled yet: the tests show where each rule decides
and where it abstains, not how often it is right.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from .extract import _PROCESS, _VARIETAL, _varietal_name

log = logging.getLogger(__name__)

RULES_VERSION = "2"

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# A month's name or its three-letter abbreviation, as a whole word: "Dec" and "December", never "declined".
_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?"
)
_HARVEST = re.compile(
    rf"\bharvest(?:ed)?\b\s*(?:months?|period|season)?\s*[:\-–]?\s*(?:in\s+|from\s+)?{_MONTH}"
    rf"(?:\s*(?:-|–|to|through|until)\s*{_MONTH})?",
    re.I,
)

# Altitude only where the text says it is one: a unit that means above sea level, or an altitude word before it.
_NUM = r"(\d{1,2}[,.]?\d{3}|\d{3,4})"
_ALTITUDE = re.compile(
    rf"\b(?:altitudes?|elevations?|grown at|alt)\b\.?[^0-9\n]{{0,20}}{_NUM}"
    rf"(?:\s*(?:-|–|to)\s*{_NUM})?\s*(?:m\b|masl\b|m\.a\.s\.l\.?|meters\b|metres\b)?"
    rf"|{_NUM}(?:\s*(?:-|–|to)\s*{_NUM})?\s*(?:masl\b|m\.a\.s\.l\.?|meters above sea level|metres above sea level)",
    re.I,
)

_DECAF = re.compile(
    r"\bdecaf|\bdecaffeinated\b|\bswiss water\b|\bmountain water\b|\bsugar ?cane\b|(?-i:\bEA\b)", re.I
)
# Honey and its relatives, as the catalog's model counts them (extract._PROCESS); matched before natural and washed,
# whose words they contain ("pulped natural", "semi-washed").
_HONEY = re.compile(
    r"\b(honey|pulped natural|semi-washed|semi washed|wet-hulled|wet hulled|giling basah)\b", re.I
)

GROWER_TYPES = (
    ("micromill", r"\bmicro-?mill\b"),
    ("washing_station", r"\bwashing station\b|\bstation\b"),
    ("cooperative", r"\bco-?operativa?\b|\bco-?op\b|\basociaci[oó]n\b|\bassociation\b"),
    ("farm", r"\bfinca\b|\bfazendas?\b|\bhacienda\b|\bestate\b|\bfarm\b|\bs[ií]tio\b"),
)
# Packaging or a placeholder, not part of a grower's name
_PACKAGING = re.compile(r"\s*\((?:grainpro|sc bags|jute[^)]*|ecotact|vacuum[^)]*)\)\s*$", re.I)
_PLACEHOLDER = re.compile(r"\bTBC\b|\bTBD\b", re.I)

# The certifications we model: (name, claimed, marked absent on the page).
CERTIFICATIONS = (
    ("organic", r"\borganic\b", r"\bnorg\b"),
    ("fair_trade", r"\bfair ?trade\b|\bFTO\b", r"\bnft\b"),
    ("rainforest_alliance", r"\brainforest\b|\bRFA\b", None),
)
# Certifications and programs we don't model: naming one makes the whole list unknown, never "none".
_OTHER_CERTS = re.compile(
    r"\bbird[- ]friendly\b|\bsmithsonian\b|\bc\.?a\.?f\.?e\.? practices\b|\butz\b|\b4c\b|\bregenerative\b|"
    r"\bdemeter\b|\bbiodynamic\b|\bkosher\b",
    re.I,
)


def process(stated: str | None, name: str | None) -> str | None:
    """washed, natural, honey or anaerobic, as the catalog's model has them (pulped natural, semi-washed and
    wet-hulled count as honey there). Decaf anywhere: None. Anaerobic or carbonic anywhere wins, as in
    extract.rules. Otherwise the importer's process column decides when it has one; without one, the name decides
    only when it names exactly one process. Several ("Honey Natural") or none: None."""
    text = f"{stated or ''} {name or ''}"
    if _DECAF.search(text):
        return None
    if re.search(_PROCESS[0][1], text, re.I):
        return "anaerobic"
    source = stated or name
    if not source:
        return None
    found = set()
    if _HONEY.search(source):
        found.add("honey")
        source = _HONEY.sub(" ", source)
    found |= {kind for kind, pat in _PROCESS[2:] if re.search(pat, source, re.I)}  # natural, washed
    return found.pop() if len(found) == 1 else None


def varieties(text: str, origin: str | None = None) -> list[str] | None:
    """The varieties the text names, in the catalog's spelling; None when it names none. Java is a variety, except
    on an Indonesian lot, where it is the island."""
    found = {_varietal_name(m.group(1)) for m in _VARIETAL.finditer(text)}
    if re.search(r"\bindonesia", f"{origin or ''} {text}", re.I) or (origin or "").strip().lower() == "java":
        found.discard("Java")
    return sorted(found) or None


def _metres(s: str) -> int | None:
    v = int(s.replace(",", "").replace(".", ""))
    return v if 300 <= v <= 3000 else None


def altitude(text: str) -> tuple[int | None, int | None]:
    """Altitude in metres as (min, max), only in an altitude context: "1,700-1,900 masl", "grown at 1800 m",
    "Altitude: 1500". (None, None) when not stated, out of the 300-3,000 m range, or reversed."""
    m = _ALTITUDE.search(text)
    if not m:
        return None, None
    lo_s, hi_s = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    lo, hi = _metres(lo_s), _metres(hi_s or lo_s)
    if lo is None or hi is None or lo > hi:
        return None, None
    return lo, hi


def _month(s: str) -> int:
    return MONTHS.index(s.lower()[:3])


def harvest_months(text: str) -> list[str] | None:
    """Harvest months as three-letter names, from "Harvest: Nov - Feb"; None when not stated. A lone "May" counts
    only in a month context (after a colon or dash, "in" or "from", or in a range): "this harvest may be smaller"
    is a verb."""
    m = _HARVEST.search(text)
    if not m:
        return None
    lead = m.group(0)[: m.start(1) - m.start(0)]
    if (
        m.group(1).lower() == "may"
        and not m.group(2)
        and not re.search(r"[:\-–]|\b(?:in|from)\b", lead, re.I)
    ):
        return None
    a = _month(m.group(1))
    b = _month(m.group(2)) if m.group(2) else a
    return [MONTHS[(a + i) % 12] for i in range((b - a) % 12 + 1)]


def grower(name: str | None) -> tuple[str | None, str | None]:
    """(type, short name) of the grower named in a lot's name, e.g. ("farm", "Fazenda Santa Lucia"). Only a
    " - "-separated part of the name with a grower word counts; the first such part decides. A placeholder name
    ("Fazenda TBC") keeps the type and leaves the name out. (None, None) when no part names a grower."""
    for part in (name or "").split(" - "):
        part = _PACKAGING.sub("", part).strip()
        for kind, pat in GROWER_TYPES:
            if re.search(pat, part, re.I):
                short = None if _PLACEHOLDER.search(part) or len(part) > 80 else part
                return kind, short
    return None, None


def certifications(text: str) -> tuple[list[str] | None, list[str] | None]:
    """(claimed, marked absent): the modeled certifications the text claims, and those it marks absent (Cafe
    Imports' "nft" and "norg": not fair trade, not organic). Each is None when there are none, so a certification the
    text says nothing about stays unknown: "nft,norg" is (None, ["fair_trade", "organic"]), never "no certifications".
    Both None (unknown) when the text names a certification or program we don't model, or claims and denies one."""
    if _OTHER_CERTS.search(text):
        return None, None
    claimed, absent = [], []
    for kind, yes, no in CERTIFICATIONS:
        c = bool(re.search(yes, text, re.I))
        d = bool(no and re.search(no, text, re.I))
        if c and d:
            return None, None
        if c:
            claimed.append(kind)
        elif d:
            absent.append(kind)
    return claimed or None, sorted(absent) or None


def _safe(rule: Callable[..., Any], *args: Any, problems: list[str], default: Any = None) -> Any:
    """A rule's value, or `default` (and a note in `problems`) when it fails on odd text: one cell never stops the
    rest."""
    try:
        return rule(*args)
    except Exception as e:  # noqa: BLE001 - a rule's failure is that value's abstention
        problems.append(f"{rule.__name__}: {type(e).__name__}")
        return default


def normalize(importer: str, cells: dict[str, str | None], problems: list[str] | None = None) -> dict:
    """Our model's values for one lot from its raw cells (column -> verbatim text). A rule that fails yields None
    and is noted in `problems`."""
    problems = [] if problems is None else problems
    if importer == "royal_coffee":
        return normalize_royal(cells, problems)
    g = cells.get
    if importer == "cafe_imports":
        name, origin = g("Name"), g("Origin")
        text = " ".join(filter(None, (g("Name"), g("Notes"))))
        stated_process, cert_text = g("Process"), g("Certification")
    else:
        name, origin, text = g("Product"), None, g("Product") or ""
        stated_process, cert_text = None, " ".join(filter(None, (g("Product"), g("Distinction"))))
    lo, hi = _safe(altitude, text, problems=problems, default=(None, None))
    kind, short = _safe(grower, name, problems=problems, default=(None, None))
    certs, not_certs = (
        _safe(certifications, cert_text, problems=problems, default=(None, None))
        if cert_text
        else (None, None)
    )
    return {
        "process_type": _safe(process, stated_process, name, problems=problems),
        "varieties": _safe(varieties, name or "", origin, problems=problems),
        "altitude_min_m": lo,
        "altitude_max_m": hi,
        "harvest_months": _safe(harvest_months, text, problems=problems),
        "grower_type": kind,
        "grower_name": short,
        "certifications": certs,  # organic, fair_trade, rainforest_alliance: claimed
        "not_certified": not_certs,  # marked absent on the page; any other stays unknown
        "rules_version": RULES_VERSION,
    }


# --- Royal Coffee (a Shopify product listing; importers.royal_cells) -----------------------------------------------
#
# Derived when the raw layer is read (importers.derive: royal_stated and normalize), never stored. Built only from
# invented products in Shopify's documented /products.json shape (tests/fixtures/importers/royal_synthetic_products.json):
# nothing here was seen on the store's site. Region, varieties, altitude and harvest read only labelled text
# ("Region: Huila", "Altitude: 1,800 masl"); lot number and warehouse read a "Lot 12345" or "<Name> Warehouse" part of
# the title; origin, process and certifications also read the title and the tags that are one word or name. Each
# abstains (None) when unsure: no label, two values that disagree, a negation, a unit or wording it doesn't know.
# Not evaluated yet (CLAUDE.md): before anything relies on these values they need reference labels from the stored
# raw layer; who may make those labels is for the owner to decide.

ROYAL_RULES_VERSION = "royal-2"

# "Label: value" in a tag or at the start of a body line.
_ROYAL_LABELS = {
    "process": r"process(?:ing)?(?: method)?",
    "region": r"region",
    "origin": r"origin|country",
    "variety": r"variet(?:y|ies|als?)|cultivars?",
    "altitude": r"altitude|elevation|growing altitude",
    "harvest": r"harvest(?: season| period| months?)?",
    "certification": r"certifications?|certified",
    "warehouse": r"warehouse",  # never "Location:", which on a coffee listing is usually the farm
}
_TITLE_PARTS = re.compile(r"\s+[-–|/]\s+|\s*\|\s*")
_LOT = re.compile(r"\blot\s*(?:no\.?|number|#)?\s*#?\s*(\d{3,7}(?:-\d{1,3})?)\b", re.I)
_YEAR = re.compile(r"(?:19|20)\d\d")  # "Harvest Lot 2025": a year, not a lot number
# A warehouse "name" that is only a preposition or a negation ("In Warehouse", "Not In Warehouse"): no name at all.
_NOT_A_NAME = re.compile(r"(?:(?:not|no(?!\.)|out of)\s+)?(?:in|at|ex|our|the|a|stored at|from)?", re.I)
_NEGATED = re.compile(r"\b(?:not|no(?!\.)|non|without|out of)\b", re.I)  # "No. 2 Warehouse" is a number
_WAREHOUSE = re.compile(
    r"^(?:(?:in|at|ex|stored at)\s+)?([A-Z][\w.' ]{1,40}?)\s+warehouse$|^warehouse\s*:?\s+([A-Z][\w.' ]{1,40})$",
    re.I,
)
# Island and state names (origins.ALIASES) that mean one origin in a coffee title; the rest (Java, Flores, Bali,
# Congo) are also a variety, a farm name or two countries, so on a title alone they abstain.
_TITLE_PLACES_OK = frozenset(
    {"sumatra", "sulawesi", "kona", "hawaii", "puerto rico", "east timor", "dr congo"}
)
_ROYAL_DECAF = re.compile(
    r"\bdecaf|\bdecaffeinated\b|\bswiss water\b|\bmountain water\b|\bsugar ?cane\b|\bethyl acetate\b|\bco2\b",
    re.I,
)
_ROYAL_HONEY = re.compile(
    r"\b(honey|pulped natural|semi-?washed|semi washed|wet-?hulled|giling basah)\b", re.I
)
_ROYAL_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?"
)
_ROYAL_HARVEST = re.compile(
    rf"^\s*{_ROYAL_MONTH}(?:\s*(?:-|–|to|through|until)\s*{_ROYAL_MONTH})?\s*(?:\d{{4}}(?:/\d{{2,4}})?)?\s*$",
    re.I,
)
_ROYAL_NUM = r"(\d{1,2},\d{3}|\d{3,4})"
_ROYAL_ALTITUDE = re.compile(
    rf"^\s*{_ROYAL_NUM}(?:\s*(?:-|–|to)\s*{_ROYAL_NUM})?\s*(?:m|masl|m\.a\.s\.l\.?|meters|metres)(?:\s+above sea level)?\.?\s*$",
    re.I,
)
_ROYAL_CERTS = (
    ("organic", r"\b(?:usda |certified )?organic\b"),
    ("fair_trade", r"\bfair ?trade(?: usa| certified)?\b"),
    ("rainforest_alliance", r"\brainforest alliance(?: certified)?\b|\bRFA\b"),
)
# "FTO" alone can mean Fair Trade Organic or only Fair Trade: it counts only alongside both spelled out.
_FTO = re.compile(r"\bFTO\b")
# A modeled certification negated ("Non-Organic", "not Fair Trade"): the claim can't be read, so abstain.
_CERT_NEGATED = re.compile(
    r"\b(?:non|not|no|without)[\s-]+(?:certified\s+)?(?:usda\s+)?(?:organic|fair ?trade|rainforest)", re.I
)


def _lines(cells: dict[str, str | None], field: str) -> list[str]:
    return [x.strip() for x in (cells.get(field) or "").split("\n") if x.strip()]


def royal_labelled(cells: dict[str, str | None], label: str) -> list[str]:
    """Every value a tag or a body line gives under `label` ("Region: Huila"), in order, each once."""
    pat = re.compile(rf"^(?:{_ROYAL_LABELS[label]})\s*[:：]\s*(.+?)\s*$", re.I)
    found: list[str] = []
    for line in _lines(cells, "tags") + _lines(cells, "body_text"):
        for part in re.split(r"(?<=[.;])\s+(?=[A-Z][a-z]+:)", line):  # "... lot. Certifications: Organic"
            if not (m := pat.match(part.strip())):
                continue
            value = m.group(1).rstrip(" .;,:")  # "Region: Huila." and "Region: Huila" agree
            if value and value not in found:
                found.append(value)
    return found


def _one(values: list[str]) -> str | None:
    """The value when every source agrees (ignoring case and spacing); None when there is none or they disagree."""
    distinct = {re.sub(r"\s+", " ", v).strip().casefold(): v.strip() for v in values if v.strip()}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def royal_lot(title: str | None) -> str | None:
    """The lot number in the title ("Lot 70412", "Lot #70588"); None when there is none, more than one, or one that
    reads as a year ("Harvest Lot 2025")."""
    found = _LOT.findall(title or "")
    if any(_YEAR.fullmatch(v) for v in found):
        return None
    return _one(found)


def royal_warehouse(cells: dict[str, str | None]) -> str | None:
    """The warehouse a title part names ("Harborview Warehouse") or a "Warehouse:" label gives; None when none does,
    two disagree, or the part is only a preposition or a negation ("In Warehouse", "Not In Warehouse")."""
    found = []
    for part in _TITLE_PARTS.split(cells.get("title") or ""):
        if m := _WAREHOUSE.match(part.strip()):
            name = (m.group(1) or m.group(2)).strip()
            if _NOT_A_NAME.fullmatch(name) or _NEGATED.search(part):
                return None
            found.append(name)
    return _one(found + royal_labelled(cells, "warehouse"))


def _process_kind(text: str) -> str | None:
    """One process in the catalog's terms (extract._PROCESS): anaerobic wins; honey and its relatives before natural
    and washed, whose words they contain; anything naming two kinds is None."""
    if re.search(_PROCESS[0][1], text, re.I):
        return "anaerobic"
    kinds = {"honey"} if _ROYAL_HONEY.search(text) else set()
    rest = _ROYAL_HONEY.sub(" ", text)
    kinds |= {kind for kind, pat in _PROCESS[2:] if re.search(pat, rest, re.I)}
    return kinds.pop() if len(kinds) == 1 else None


def royal_process(cells: dict[str, str | None]) -> str | None:
    """washed, natural, honey or anaerobic. A decaf anywhere (title, tags, type, body) abstains. Anaerobic (or
    carbonic) in a label, the title or a process tag wins, as in the catalog's rule (extract.rules). Otherwise
    "Process:" labels decide when there are any (all must agree); else the title and the tags that are a process
    word and nothing else ("Natural"), which must name one process between them."""
    everything = " ".join(
        filter(None, (cells.get(f) for f in ("title", "tags", "product_type", "body_text")))
    )
    if _ROYAL_DECAF.search(everything):
        return None
    labelled = royal_labelled(cells, "process")
    plain = [t for t in _lines(cells, "tags") if ":" not in t and _process_kind(t) and len(t.split()) <= 3]
    title = cells.get("title") or ""
    if re.search(_PROCESS[0][1], " ".join([title, *plain, *labelled]), re.I):
        return "anaerobic"
    if labelled:
        kinds = {_process_kind(v) for v in labelled}
        return kinds.pop() if len(kinds) == 1 else None
    kinds = {k for k in (_process_kind(title), *map(_process_kind, plain)) if k}
    # the title named two processes (_process_kind None) but a tag named one: still unclear
    if not _process_kind(title) and any(re.search(pat, title, re.I) for _, pat in _PROCESS[1:]):
        return None
    return kinds.pop() if len(kinds) == 1 else None


def royal_origin(cells: dict[str, str | None]) -> str | None:
    """The country an "Origin:" label gives (read through origins.canonical_country, so "Origin: Sumatra" is
    Indonesia), else the one country the title names. A title reads the catalog's country names (origins.COUNTRIES),
    but of the island and state names among them only those that mean one place in a coffee title
    (_TITLE_PLACES_OK): "Java" is also a variety and "Flores" a common farm name, so "Java Natural" or "Finca Las
    Flores" never makes Indonesia. None when unclear."""
    from .origins import ALIASES, COUNTRIES, canonical_country

    labelled = royal_labelled(cells, "origin")
    if labelled:
        names = {canonical_country(v) for v in labelled}
        return names.pop() if len(names) == 1 and names <= set(COUNTRIES) else None
    title = cells.get("title") or ""
    found = {
        canonical_country(c)
        for c in COUNTRIES
        if (c.lower() not in ALIASES or c.lower() in _TITLE_PLACES_OK)
        and re.search(rf"\b{re.escape(c)}\b", title, re.I)
    }
    return found.pop() if len(found) == 1 else None


def royal_region(cells: dict[str, str | None]) -> str | None:
    """The region a "Region:" label gives, as written; None without one or when two disagree."""
    return _one(royal_labelled(cells, "region"))


def royal_varieties(cells: dict[str, str | None]) -> list[str] | None:
    """The varieties a "Variety:" label names, in the catalog's spelling; None without a label, or when a label
    names something the catalog doesn't know (the list would be incomplete)."""
    labelled = royal_labelled(cells, "variety")
    if not labelled:
        return None
    found: set[str] = set()
    for value in labelled:
        names = {_varietal_name(m.group(1)) for m in _VARIETAL.finditer(value)}
        leftover = re.sub(r"[\s,;&/+]|\band\b", "", _VARIETAL.sub("", value), flags=re.I)
        if not names or leftover:
            return None
        found |= names
    return sorted(found)


def _royal_metres(s: str) -> int | None:
    v = int(s.replace(",", ""))
    return v if 300 <= v <= 3000 else None


def royal_altitude(cells: dict[str, str | None]) -> tuple[int | None, int | None]:
    """(min, max) metres from an "Altitude:" label that is a number or range in metres and nothing else; (None, None)
    without one, in feet, outside 300-3,000 m, reversed, or when two labels disagree."""
    value = _one(royal_labelled(cells, "altitude"))
    m = _ROYAL_ALTITUDE.match(value or "")
    if not m:
        return None, None
    lo, hi = _royal_metres(m.group(1)), _royal_metres(m.group(2) or m.group(1))
    if lo is None or hi is None or lo > hi:
        return None, None
    return lo, hi


def royal_harvest_months(cells: dict[str, str | None]) -> list[str] | None:
    """Harvest months as three-letter names from a "Harvest:" label that is one month or a range ("October -
    January", optionally with its year); None for anything else ("2025/26", several ranges, prose)."""
    m = _ROYAL_HARVEST.match(_one(royal_labelled(cells, "harvest")) or "")
    if not m:
        return None
    a = MONTHS.index(m.group(1)[:3].lower())
    b = MONTHS.index(m.group(2)[:3].lower()) if m.group(2) else a
    return [MONTHS[(a + i) % 12] for i in range((b - a) % 12 + 1)]


def royal_certifications(cells: dict[str, str | None]) -> list[str] | None:
    """The certifications claimed (normalize's `certifications`; the claimed half of the claimed/absent split) by
    "Certification:" labels, by tags that are a certification and nothing else, and by the title. None when nothing
    claims one; when a label, a tag or the title names a program we don't model (_OTHER_CERTS: the list would be
    incomplete) or negates a modeled one ("Non-Organic"); and for a bare "FTO" unless fair trade and organic are both
    claimed outright. The store marks no certification absent, so Royal's `not_certified` is always None: a
    certification it doesn't claim is unknown."""
    labels = royal_labelled(cells, "certification")
    plain_tags = [t for t in _lines(cells, "tags") if ":" not in t]
    title = cells.get("title") or ""
    if any(_OTHER_CERTS.search(t) or _CERT_NEGATED.search(t) for t in [*labels, *plain_tags, title]):
        return None
    fto = any(_FTO.search(t) for t in [*labels, *plain_tags, title])
    found: set[str] = set()
    for value in labels:
        rest = _FTO.sub("", value)
        for kind, pat in _ROYAL_CERTS:
            if re.search(pat, value, re.I):
                found.add(kind)
                rest = re.sub(pat, "", rest, flags=re.I)
        if re.sub(r"[\s,;&/+.]|\band\b|\bcertified\b", "", rest, flags=re.I):
            return None
    sources = [*plain_tags, title]
    for kind, pat in _ROYAL_CERTS:
        if any(
            re.search(pat, t, re.I) and (t is sources[-1] or re.fullmatch(rf"\s*(?:{pat})\s*", t, re.I))
            for t in sources
        ):
            found.add(kind)
    if fto and not {"organic", "fair_trade"} <= found:
        return None
    order = [k for k, _ in _ROYAL_CERTS]
    return sorted(found, key=order.index) or None


def royal_stated(cells: dict[str, str | None], problems: list[str]) -> dict:
    """The fields a Royal product states, as importers.stated gives them for the other importers: its title as the
    name, and the lot number, warehouse, origin, region and process its title and labels state (None when unclear).
    A Shopify listing has no status, arrival, bag count or bag size column."""
    return {
        "lot_id": _safe(royal_lot, cells.get("title"), problems=problems),
        "name": " ".join((cells.get("title") or "").split()) or None,
        "origin": _safe(royal_origin, cells, problems=problems),
        "region": _safe(royal_region, cells, problems=problems),
        "process": _safe(lambda c: _one(royal_labelled(c, "process")), cells, problems=problems),
        "status": None,
        "est_arrival": None,
        "warehouse": _safe(royal_warehouse, cells, problems=problems),
        "bags": None,
        "bag_size": None,
        "bag_kg": None,
    }


def normalize_royal(cells: dict[str, str | None], problems: list[str]) -> dict:
    """Our values for one Royal product from its raw cells (importers.royal_cells: field -> verbatim text), with the
    same keys as normalize gives the other importers. Royal's titles name no grower we model, so those are None."""
    lo, hi = _safe(royal_altitude, cells, problems=problems, default=(None, None))
    return {
        "process_type": _safe(royal_process, cells, problems=problems),
        "varieties": _safe(royal_varieties, cells, problems=problems),
        "altitude_min_m": lo,
        "altitude_max_m": hi,
        "harvest_months": _safe(royal_harvest_months, cells, problems=problems),
        "grower_type": None,
        "grower_name": None,
        "certifications": _safe(royal_certifications, cells, problems=problems),  # claimed
        # the store never marks a certification absent, so none is ever "not certified": unknown, not absent
        "not_certified": None,
        "rules_version": ROYAL_RULES_VERSION,
    }
