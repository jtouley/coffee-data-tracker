"""Our model's values for an importer's lot, derived from its raw page text (the importer_raw cells).

Deterministic rules, and each one abstains (None) when the text doesn't settle it: a value we can't tell is left
empty, never guessed. Process, varieties and altitude use the catalog's own vocabularies (extract.py), so an
importer lot and a roaster listing are described in the same terms.

These rules' quality is "how often is it right", so before anything relies on them they get a CLAUDE.md evaluation
(held-out split, double labels, Wilson intervals). None has been labeled yet: the fixtures' tests show where each
rule decides and where it abstains, not how often it is right.
"""

from __future__ import annotations

import re

from .extract import _MASL, _PROCESS, _VARIETAL, _varietal_name
from .extract import _int as _masl

RULES_VERSION = "1"

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_HARVEST = re.compile(
    rf"\bharvest(?:ed)?\b\s*[:\-–]?\s*(?:in\s+)?{_MONTH}(?:\s*(?:-|–|to|through)\s*{_MONTH})?", re.I
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

CERTIFICATIONS = (
    ("organic", r"\borganic\b", r"\bnorg\b"),
    ("fair_trade", r"\bfair ?trade\b|\bFTO\b", r"\bnft\b"),
    ("rainforest_alliance", r"\brainforest\b|\bRFA\b", None),
)


def process(stated: str | None, name: str | None) -> str | None:
    """washed, natural, honey or anaerobic, as the catalog's model has them (wet-hulled counts as honey there).
    Anaerobic or carbonic anywhere wins, as in extract.rules. Otherwise the importer's own process column decides
    when it has one; without one, the name decides only when it names exactly one process. Decaf, several
    processes ("Honey Natural") or none: None."""
    text = f"{stated or ''} {name or ''}"
    if re.search(_PROCESS[0][1], text, re.I):
        return "anaerobic"
    for source in (stated, None if stated else name):
        if not source:
            continue
        found = {kind for kind, pat in _PROCESS[1:] if re.search(pat, source, re.I)}
        return found.pop() if len(found) == 1 else None
    return None


def varieties(text: str, origin: str | None = None) -> list[str] | None:
    """The varieties the text names, in the catalog's spelling; None when it names none. "Java" is left out when
    the lot is Indonesian (it is the island there)."""
    found = {_varietal_name(m.group(1)) for m in _VARIETAL.finditer(text)}
    if re.search(r"\bindonesia|\bjava\b", f"{origin or ''} {text}", re.I) and not re.search(
        r"\bjava variety\b", text, re.I
    ):
        found.discard("Java")
    return sorted(found) or None


def altitude(text: str) -> tuple[int | None, int | None]:
    """Altitude in metres as (min, max) from "1,700-1,900 masl" or "1800 m"; (None, None) when not stated or out
    of the 300-3,000 m range."""
    m = _MASL.search(text)
    if not m:
        return None, None
    lo, hi = _masl(m.group(1)), _masl(m.group(2) or m.group(1))
    if lo is None or hi is None or lo > hi:
        return None, None
    return lo, hi


def harvest_months(text: str) -> list[str] | None:
    """Harvest months as three-letter names, from "Harvest: Nov - Feb"; None when not stated."""
    m = _HARVEST.search(text)
    if not m:
        return None
    a = MONTHS.index(m.group(1).lower())
    b = MONTHS.index(m.group(2).lower()) if m.group(2) else a
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


def certifications(text: str, states_absence: bool) -> list[str] | None:
    """The certifications the text claims. `states_absence`: the source marks a missing one (Cafe Imports' "nft",
    "norg"), so an empty list means "none"; otherwise no mention is None (unknown). A certification both claimed
    and marked absent is a contradiction: None for the whole list."""
    found = []
    for kind, yes, no in CERTIFICATIONS:
        claimed = bool(re.search(yes, text, re.I))
        denied = bool(no and re.search(no, text, re.I))
        if claimed and denied:
            return None
        if claimed:
            found.append(kind)
    return found if found or states_absence else None


def normalize(importer: str, raw: dict[str, str | None], origin: str | None = None) -> dict:
    """Our model's values for one lot from its raw cells (importer_raw.cells: column -> verbatim text)."""
    if importer == "royal_coffee":
        return normalize_royal(raw)
    g = raw.get
    if importer == "cafe_imports":
        name, text = g("Name"), " ".join(filter(None, (g("Name"), g("Notes"))))
        stated_process = g("Process")
        certs = certifications(g("Certification") or "", states_absence=bool(g("Certification")))
    else:
        name, text = g("Product"), g("Product") or ""
        stated_process = None
        certs = certifications(text, states_absence=False)
    lo, hi = altitude(text)
    kind, short = grower(name)
    return {
        "process_type": process(stated_process, name),
        "varieties": varieties(name or "", origin),
        "altitude_min_m": lo,
        "altitude_max_m": hi,
        "harvest_months": harvest_months(text),
        "grower_type": kind,
        "grower_name": short,
        "certifications": certs,
        "rules_version": RULES_VERSION,
    }


# --- Royal Coffee (a Shopify product listing; importers.royal_cells) -----------------------------------------------
#
# Derived when the raw layer is read (importers.derive), never stored. Built only from invented products in Shopify's
# documented /products.json shape (tests/fixtures/importers/royal_synthetic_products.json): nothing here was seen on
# the store's site, so every rule reads only text that labels itself ("Process: Washed", "Altitude: 1,800 masl", a
# "Lot 12345" or "<Name> Warehouse" part of the title) and abstains (None) on anything else: no label, two labels
# that disagree, a unit or wording it doesn't know. Not evaluated yet (CLAUDE.md): before anything relies on these
# values they need labels from the real raw layer, which only a person (never an AI agent) may look at.

ROYAL_RULES_VERSION = "royal-1"

# "Label: value" in a tag or at the start of a body line.
_ROYAL_LABELS = {
    "process": r"process(?:ing)?(?: method)?",
    "region": r"region",
    "origin": r"origin|country",
    "variety": r"variet(?:y|ies|als?)|cultivars?",
    "altitude": r"altitude|elevation|growing altitude",
    "harvest": r"harvest(?: season| period| months?)?",
    "certification": r"certifications?|certified",
    "warehouse": r"warehouse|location",
}
_TITLE_PARTS = re.compile(r"\s+[-–|/]\s+|\s*\|\s*")
_LOT = re.compile(r"\blot\s*(?:no\.?|number|#)?\s*#?\s*(\d{3,7}(?:-\d{1,3})?)\b", re.I)
_WAREHOUSE = re.compile(
    r"^(?:(?:in|at|ex|stored at)\s+)?([A-Z][\w.' ]{1,40}?)\s+warehouse$|^warehouse\s*:?\s+([A-Z][\w.' ]{1,40})$",
    re.I,
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
_NUM = r"(\d{1,2},\d{3}|\d{3,4})"
_ROYAL_ALTITUDE = re.compile(
    rf"^\s*{_NUM}(?:\s*(?:-|–|to)\s*{_NUM})?\s*(?:m|masl|m\.a\.s\.l\.?|meters|metres)(?:\s+above sea level)?\.?\s*$",
    re.I,
)
_ROYAL_CERTS = (
    ("organic", r"\b(?:usda |certified )?organic\b"),
    ("fair_trade", r"\bfair ?trade(?: usa| certified)?\b|\bFTO\b"),
    ("rainforest_alliance", r"\brainforest alliance(?: certified)?\b|\bRFA\b"),
)


def _lines(cells: dict[str, str | None], field: str) -> list[str]:
    return [x.strip() for x in (cells.get(field) or "").split("\n") if x.strip()]


def royal_labelled(cells: dict[str, str | None], label: str) -> list[str]:
    """Every value a tag or a body line gives under `label` ("Region: Huila"), in order, each once."""
    pat = re.compile(rf"^(?:{_ROYAL_LABELS[label]})\s*[:：]\s*(.+?)\s*$", re.I)
    found: list[str] = []
    for line in _lines(cells, "tags") + _lines(cells, "body_text"):
        for part in re.split(r"(?<=[.;])\s+(?=[A-Z][a-z]+:)", line):  # "... lot. Certifications: Organic"
            if (m := pat.match(part.strip())) and m.group(1) not in found:
                found.append(m.group(1))
    return found


def _one(values: list[str]) -> str | None:
    """The value when every source agrees (ignoring case and spacing); None when there is none or they disagree."""
    distinct = {re.sub(r"\s+", " ", v).strip().casefold(): v.strip() for v in values if v.strip()}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def royal_lot(title: str | None) -> str | None:
    """The lot number in the title ("Lot 70412", "Lot #70588"); None when there is none or more than one."""
    return _one(_LOT.findall(title or ""))


def royal_warehouse(cells: dict[str, str | None]) -> str | None:
    """The warehouse a title part names ("Harborview Warehouse") or a "Warehouse:" label gives; None when none does
    or two disagree."""
    found = []
    for part in _TITLE_PARTS.split(cells.get("title") or ""):
        if m := _WAREHOUSE.match(part.strip()):
            found.append(m.group(1) or m.group(2))
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
    """washed, natural, honey or anaerobic. A decaf anywhere (title, tags, type, body) abstains. "Process:" labels
    decide when there are any (all must agree); otherwise the title and the tags that are a process word and nothing
    else ("Natural"), which must name one process between them."""
    everything = " ".join(
        filter(None, (cells.get(f) for f in ("title", "tags", "product_type", "body_text")))
    )
    if _ROYAL_DECAF.search(everything):
        return None
    labelled = royal_labelled(cells, "process")
    if labelled:
        kinds = {_process_kind(v) for v in labelled}
        return kinds.pop() if len(kinds) == 1 else None
    plain = [t for t in _lines(cells, "tags") if ":" not in t and _process_kind(t) and len(t.split()) <= 3]
    title = cells.get("title") or ""
    if re.search(_PROCESS[0][1], " ".join([title, *plain]), re.I):
        return "anaerobic"
    kinds = {k for k in (_process_kind(title), *map(_process_kind, plain)) if k}
    # the title named two processes (_process_kind None) but a tag named one: still unclear
    if not _process_kind(title) and any(re.search(pat, title, re.I) for _, pat in _PROCESS[1:]):
        return None
    return kinds.pop() if len(kinds) == 1 else None


def royal_origin(cells: dict[str, str | None]) -> str | None:
    """The country an "Origin:" label gives, else the one country the title names; None when unclear."""
    from .origins import COUNTRIES, canonical_country

    labelled = royal_labelled(cells, "origin")
    if labelled:
        names = {canonical_country(v) for v in labelled}
        return names.pop() if len(names) == 1 and names <= set(COUNTRIES) else None
    title = cells.get("title") or ""
    found = {c for c in COUNTRIES if re.search(rf"\b{re.escape(c)}\b", title, re.I)}
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


def _metres(s: str) -> int | None:
    v = int(s.replace(",", ""))
    return v if 300 <= v <= 3000 else None


def royal_altitude(cells: dict[str, str | None]) -> tuple[int | None, int | None]:
    """(min, max) metres from an "Altitude:" label that is a number or range in metres and nothing else; (None, None)
    without one, in feet, outside 300-3,000 m, reversed, or when two labels disagree."""
    value = _one(royal_labelled(cells, "altitude"))
    m = _ROYAL_ALTITUDE.match(value or "")
    if not m:
        return None, None
    lo, hi = _metres(m.group(1)), _metres(m.group(2) or m.group(1))
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
    """The certifications claimed by "Certification:" labels, by tags that are a certification and nothing else, and
    by the title. None when nothing claims one (the store doesn't mark absence, so that's unknown, not "none"), or
    when a label names a program we don't model (the list would be incomplete)."""
    found: set[str] = set()
    for value in royal_labelled(cells, "certification"):
        rest = value
        for kind, pat in _ROYAL_CERTS:
            if re.search(pat, value, re.I):
                found.add(kind)
                rest = re.sub(pat, "", rest, flags=re.I)
        if re.sub(r"[\s,;&/+.]|\band\b|\bcertified\b", "", rest, flags=re.I):
            return None
    sources = [t for t in _lines(cells, "tags") if ":" not in t] + [cells.get("title") or ""]
    for kind, pat in _ROYAL_CERTS:
        if any(
            re.search(pat, t, re.I) and (t is sources[-1] or re.fullmatch(rf"\s*(?:{pat})\s*", t, re.I))
            for t in sources
        ):
            found.add(kind)
    order = [k for k, _ in _ROYAL_CERTS]
    return sorted(found, key=order.index) or None


def normalize_royal(cells: dict[str, str | None]) -> dict:
    """Our values for one Royal product from its raw cells (importers.royal_cells: field -> verbatim text)."""
    lo, hi = royal_altitude(cells)
    return {
        "lot_id": royal_lot(cells.get("title")),
        "warehouse": royal_warehouse(cells),
        "origin": royal_origin(cells),
        "region": royal_region(cells),
        "process_type": royal_process(cells),
        "varieties": royal_varieties(cells),
        "altitude_min_m": lo,
        "altitude_max_m": hi,
        "harvest_months": royal_harvest_months(cells),
        "certifications": royal_certifications(cells),
        "rules_version": ROYAL_RULES_VERSION,
    }
