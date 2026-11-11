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
