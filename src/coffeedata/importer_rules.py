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
