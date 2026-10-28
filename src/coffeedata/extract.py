"""Structured coffee attributes: deterministic rules first, Claude Haiku (optional) for what rules miss."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter

from .models import CatalogItem, Enrichment
from .origins import COUNTRIES, REGIONS, canonical_country

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"

_PROCESS = [
    ("anaerobic", r"anaerobic|carbonic|thermal shock|co-?ferment"),
    ("honey", r"\b(honey|pulped natural|semi-washed|wet-hulled|giling basah)\b"),
    ("natural", r"\b(natural|dry process(ed)?|sun-?dried)\b"),
    ("washed", r"\b(washed|wet process(ed)?|fully washed)\b"),
]
_ROAST = [
    ("light", r"\b(light|light-medium|nordic)\b[ -]?roast|roast(ed)?[ :-]*light|roastlevel__light"),
    ("dark", r"\b(dark|french|italian)\b[ -]?roast|roast(ed)?[ :-]*dark|roastlevel__dark"),
    ("medium", r"\b(medium|medium-dark|city)\b[ -]?roast|roast(ed)?[ :-]*medium|roastlevel__medium"),
]
VARIETALS = [
    "Gesha",
    "Geisha",
    "Bourbon",
    "Pink Bourbon",
    "Red Bourbon",
    "Yellow Bourbon",
    "Typica",
    "Caturra",
    "Catuai",
    "Catuaí",
    "Pacamara",
    "Pacas",
    "SL28",
    "SL34",
    "Ruiru 11",
    "Batian",
    "Castillo",
    "Heirloom",
    "74110",
    "74112",
    "74158",
    "Wush Wush",
    "Sidra",
    "Maragogype",
    "Mundo Novo",
    "Villa Sarchi",
    "Java",
    "Catimor",
    "Sarchimor",
    "Parainema",
    "Obata",
    "Laurina",
    "Eugenioides",
    "Mokka",
    "Tabi",
]
_VARIETAL = re.compile(r"\b(" + "|".join(re.escape(v) for v in VARIETALS) + r")\b", re.I)
_COUNTRY = re.compile(r"\b(" + "|".join(re.escape(c) for c in COUNTRIES) + r")\b", re.I)
DEMONYMS = {
    "kenyan": "Kenya",
    "ethiopian": "Ethiopia",
    "colombian": "Colombia",
    "guatemalan": "Guatemala",
    "rwandan": "Rwanda",
    "brazilian": "Brazil",
    "honduran": "Honduras",
    "peruvian": "Peru",
    "mexican": "Mexico",
    "burundian": "Burundi",
    "ugandan": "Uganda",
    "salvadoran": "El Salvador",
    "salvadorian": "El Salvador",
    "salvadorean": "El Salvador",
    "costa rican": "Costa Rica",
    "panamanian": "Panama",
    "nicaraguan": "Nicaragua",
    "indonesian": "Indonesia",
    "tanzanian": "Tanzania",
    "yemeni": "Yemen",
    "ecuadorian": "Ecuador",
    "bolivian": "Bolivia",
    "congolese": "Democratic Republic of the Congo",
    "sumatran": "Indonesia",
    "javanese": "Indonesia",
    "hawaiian": "United States",
    "papuan": "Papua New Guinea",
}
_DEMONYM = re.compile(r"\b(" + "|".join(DEMONYMS) + r")\b", re.I)
_MASL = re.compile(
    r"(\d[\d,.]{2,5})\s*(?:[-–to]+\s*(\d[\d,.]{2,5}))?\s*(?:masl|m\.?a\.?s\.?l|meters|metres|m\b)", re.I
)
_NOTES = re.compile(
    r"(?:tasting notes?|flavor notes?|cup notes?|notes? of|we taste|tastes like|notes?)\s*[:\-–]\s*"
    r"([^\n.]{3,160})",
    re.I,
)


def _int(s: str | None) -> int | None:
    if not s:
        return None
    v = int(float(s.replace(",", "")))
    return v if 300 <= v <= 3000 else None


_FILLER_LEAD = re.compile(
    r"^(a|an|the|and|with|hints? of|notes? of|touch of|bit of|some|lots of|lingering|"
    r"bright|sweet and|juicy and)\s+"
)
_FILLER_TAIL = re.compile(r"\s+(notes?|taste|tastes|flavou?rs?|finish|aftertaste|undertones?|tones?|vibes?)$")


def split_notes(s: str) -> list[str]:
    """'Blackberry, Milk Chocolate & a hint of lime notes' -> ['blackberry', 'milk chocolate', 'lime']."""
    out: list[str] = []
    for part in re.split(r",|\||/|•|·|;|\band\b|&|\+", s):
        n = re.sub(r"[^a-z' -]", "", part.strip().lower()).strip(" -'")
        for _ in range(2):
            n = _FILLER_LEAD.sub("", n)
        n = _FILLER_TAIL.sub("", n).strip()
        if 2 < len(n) <= 25 and len(n.split()) <= 3 and n not in out:
            out.append(n)
    return out[:8]


def tag_values(tags: list[str]) -> dict[str, list[str]]:
    """Structured tags like 'origin:Panama', 'roastlevel__light-roast', 'process: Natural'."""
    kv: dict[str, list[str]] = {}
    for t in tags:
        m = re.match(r"\s*([A-Za-z _-]{2,20}?)\s*(?:__|:)\s*(.+)", t)
        if m:
            key = re.sub(r"[^a-z]", "", m.group(1).lower())
            kv.setdefault(key, []).append(m.group(2).replace("-", " ").strip())
    return kv


def _first(kv: dict[str, list[str]], *keys: str) -> str:
    return " ".join(v for k in keys for v in kv.get(k, []))


# "Java" is an origin only next to a Javanese place or estate. "Mocha Java" is a blend, and "java" alone is slang
# for coffee ("Java Chip", "Java Jingle").
_JAVA_ORIGIN = re.compile(
    r"\b((west|east|central) java|java (estate|sunda|preanger|ijen|frinsa|kayumas|blawan|jampit|taman|garut|"
    r"halu|island|arabica)|indonesia)\b",
    re.I,
)
_MOCHA_JAVA = re.compile(r"\bmo[ck]h?a[- ]java\b", re.I)
# Grab-bag offers name no single origin: "Roaster Dave's Pick (Random Coffee)", "Mystery Coffee".
_NO_ORIGIN = re.compile(r"\b(random|mystery|surprise|roaster'?s pick|roaster'?s choice)\b", re.I)


def _strip_java(text: str) -> str:
    """Blank out "java" (keeping offsets) unless the text names a Javanese place."""
    if _JAVA_ORIGIN.search(text):
        return text
    return re.sub(r"\bjava\b", lambda m: " " * len(m.group()), text, flags=re.I)


def _countries(text: str) -> list[str]:
    text = _strip_java(text)
    found = [canonical_country(m.group(1)) for m in _COUNTRY.finditer(text)]
    if not found and (m := _DEMONYM.search(text)):
        found = [DEMONYMS[m.group(1).lower()]]
    return list(dict.fromkeys(found))


def _country(text: str) -> str | None:
    found = _countries(text)
    return found[0] if found else None


# --- Origin in a description ------------------------------------------------------------------------
# A description names countries for reasons other than where the coffee grew: where its variety was bred ("SL-28, a
# variety developed in Kenya"), or another coffee. Each mention is weighed by its context, and the country the
# description names as the origin wins over the first one it names.
_REGION = re.compile(
    r"\b(" + "|".join(re.escape(r) for r in sorted(REGIONS, key=len, reverse=True)) + r")\b"
    # Antigua and Barbuda grows no coffee to speak of; Santa Barbara, California is a roaster's home, not a farm's.
    r"(?!\s*(?:and|&)\s+barbuda|,?\s+(?:ca|california)\b)",
    re.I,
)
_VARIETY_NAME = re.compile(_VARIETAL.pattern + r"|\bsl[- ]?\d\d\b|\bruiru\b", re.I)
# Where a variety comes from: "developed in Kenya", "bred by Scott Labs in Kenya", "native to southwestern Ethiopia".
_BRED_IN = re.compile(
    r"\b(?:developed|bred|native|indigenous|descend(?:s|ed|ing|ant)?)\b(?:\s+[\w'’.-]+){0,4}?\s+(?:in|to|from|of)\s+"
    r"(?:[\w'’-]+\s+){0,3}$",
    re.I,
)
# The same with words a coffee's own origin uses too ("originates in Huila"): only when the sentence names a variety.
_FROM_ORIGINALLY = re.compile(
    r"\b(?:originally|originat(?:ed|es|ing)|discovered|first (?:found|grown|planted|identified|selected))\b"
    r"(?:\s+[\w'’.-]+){0,4}?\s+(?:in|to|from|of)\s+(?:[\w'’-]+\s+){0,3}$",
    re.I,
)
# "a Kenyan variety", "Kenya-bred"
_VARIETY_AFTER = re.compile(
    r"^(?:[- ]?(?:bred|developed)\b|\s+(?:[\w-]+\s+)?(?:variet(?:y|al|ies)|cultivars?|landraces?)\b)", re.I
)
# "Origin: Honduras", "Region: Marcala, Honduras"
_LABEL = re.compile(r"\b(?:origin|country|region|location)\s*[:\-–]\s*(?:[^\n,:;]{1,40},\s*)?$", re.I)
# "Marcala, Honduras", "Finca El Puente, Honduras" (case-sensitive: a place name before the comma)
_PLACE_COMMA = re.compile(r"\b([A-Z][\w'’.-]*(?:\s+(?:de|del|la|el)?\s*[A-Z][\w'’.-]*){0,3}),\s*$")
# "a farm in Honduras", "grown at 1,600 masl in Honduras", "cultivated in Honduras"
_LOCATED = re.compile(
    r"\b(?:farms?|fincas?|estates?|stations?|cooperatives?|co-?ops?|located|grow(?:n|s|ing)?|produced|cultivat\w*|"
    r"harvested|sourced)\b[^.;!?\n]{0,50}?\b(?:in|at|from)\s+(?:the\s+)?(?:[\w'’-]+\s+){0,2}$",
    re.I,
)
ORIGIN, PLAIN, WEAK, BRED = "origin", "plain", "weak", "bred"


def _mention_kind(text: str, start: int, end: int) -> str:
    """How a country (or demonym) at text[start:end] relates to the coffee: its origin, a plain mention, a weak one
    (next to a variety name), or where a variety was bred (which never counts)."""
    before = re.split(r"[.!?;\n]", text[max(0, start - 120) : start])[-1]
    after = re.split(r"[.!?;\n]", text[end : end + 60])[0]
    near_variety = bool(_VARIETY_NAME.search(before[-40:]) or _VARIETY_NAME.search(after[:30]))
    if _BRED_IN.search(before) or (
        _FROM_ORIGINALLY.search(before) and (near_variety or _VARIETY_NAME.search(before + after))
    ):
        return BRED
    if _LABEL.search(before):
        return ORIGIN
    if (m := _PLACE_COMMA.search(before)) and not (
        _COUNTRY.search(m.group(1)) or _DEMONYM.search(m.group(1)) or _VARIETY_NAME.search(m.group(1))
    ):
        return ORIGIN
    if near_variety or _VARIETY_AFTER.search(after):
        return WEAK
    return ORIGIN if _LOCATED.search(before) else PLAIN


def _top(names: list[str]) -> str | None:
    """The most-named country, the first named on a tie."""
    if not names:
        return None
    counts = Counter(names)
    return max(dict.fromkeys(names), key=lambda n: counts[n])


def _region(text: str) -> str | None:
    m = _REGION.search(text)
    return REGIONS[m.group(1).lower()] if m else None


def _body_mentions(body: str) -> dict[str, list[str]]:
    text = _strip_java(body)
    out: dict[str, list[str]] = {ORIGIN: [], PLAIN: [], WEAK: [], "demonym": [], "weak_demonym": []}
    mentions: list[tuple[str, str, int]] = []
    for m in _COUNTRY.finditer(text):
        kind = _mention_kind(text, m.start(), m.end())
        mentions.append((kind, canonical_country(m.group(1)), m.start()))
    for m in _DEMONYM.finditer(text[:1500]):
        kind = _mention_kind(text, m.start(), m.end())
        mentions.append((kind, DEMONYMS[m.group(1).lower()], -1))
    bred = {c for kind, c, _ in mentions if kind == BRED}
    for kind, c, start in mentions:
        if kind == BRED:
            continue
        # Once a country is where the variety was bred, its other passing mentions are that story too ("Kenya
        # remains its best-known home"); only a labelled or located origin still counts for it.
        if kind == PLAIN and c in bred:
            kind = WEAK
        if start < 0:
            out["weak_demonym" if kind == WEAK else "demonym"].append(c)
        # A labelled or located origin counts anywhere in the description; other mentions only near its top.
        elif kind == ORIGIN or start < 1500:
            out[kind].append(c)
    return out


def origin_country(title: str, body: str) -> str | None:
    """The origin a listing names: a country in the title; else the country the description names as the origin,
    else the one it names most; else a growing region that belongs to one country; else, as a last resort, a country
    named only beside a variety. A country named only as where a variety was bred is never the origin."""
    if c := _country(title):
        return c
    found = _body_mentions(body)
    return (
        _top(found[ORIGIN])
        or _top(found[PLAIN])
        or _top(found["demonym"])
        or _region(title)
        or _region(body)
        or _top(found[WEAK])
        or _top(found["weak_demonym"])
    )


def _process(text: str) -> str | None:
    for name, pat in _PROCESS:
        if re.search(pat, text, re.I):
            return name
    return None


def rules(item: CatalogItem) -> Enrichment:
    """Cheap, deterministic extraction from structured tags, title, then labelled body text."""
    kv = tag_values(item.tags)
    tags = " ".join(item.tags)
    body = item.body_text[:4000]
    e = Enrichment(body_hash=item.body_hash)

    origin_tag = _first(kv, "origin", "country", "region")
    # A title naming several countries ("Puerto Rico Supremo, Mexico, or Colombia") is a choice or a blend.
    no_origin = len(_countries(item.title)) > 1 or bool(_NO_ORIGIN.search(item.title))
    e.origin_country = (
        _country(origin_tag)
        or _region(origin_tag)
        or (None if no_origin else origin_country(item.title, body))
    )
    labelled = re.search(r"process(?:ing)?\s*[:\-–]\s*([^\n]{2,40})", body, re.I)
    e.process = (
        _process(_first(kv, "process", "processing"))
        or _process(item.title)  # type: ignore[assignment]
        or (_process(labelled.group(1)) if labelled else None)
    )
    roast_text = _first(kv, "roast", "roastlevel", "roastprofile")
    for name, pat in _ROAST:
        if re.search(rf"\b{name}\b", roast_text, re.I) or re.search(
            pat, f"{item.title} {tags}\n{body}", re.I
        ):
            e.roast_level = name  # type: ignore[assignment]
            break
    e.varietals = sorted(
        {
            v.group(1).title()
            for v in _VARIETAL.finditer(f"{item.title} {_first(kv, 'variety', 'varietal')} {body}")
        }
    )[:6]
    if m := _MASL.search(body):
        e.elevation_masl_min, e.elevation_masl_max = _int(m.group(1)), _int(m.group(2) or m.group(1))
    if notes := _first(kv, "notes", "note", "flavor", "flavors", "flavornotes", "tastingnotes", "tasting"):
        e.tasting_notes = split_notes(notes.replace(" ", ",") if "," not in notes else notes)
    elif m := _NOTES.search(body):
        e.tasting_notes = split_notes(m.group(1))
    head = f"{item.title} {tags}"
    e.is_decaf = bool(re.search(r"\bdecaf", head, re.I) or "decaf" in _first(kv, "caffeine").lower())
    origin_kind = _first(kv, "origin", "type").lower()
    e.is_blend = bool(
        re.search(r"\bblend\b", item.title, re.I) or _MOCHA_JAVA.search(item.title) or "blend" in origin_kind
    )
    if e.is_blend and not re.search(r"single", origin_kind):
        e.origin_country = e.origin_country if _country(item.title) else None
    return e


def needs_llm(e: Enrichment) -> bool:
    return not (e.origin_country or e.is_blend) or not e.process or not e.tasting_notes


# --- Claude Haiku -----------------------------------------------------------------------------------

SYSTEM = (
    "You extract structured attributes of a retail coffee product from a roaster's product listing. "
    "Use only facts stated in the listing; use null or [] when the listing doesn't say. "
    "tasting_notes: short lowercase singular descriptors as the roaster lists them (e.g. 'blueberry', "
    "'milk chocolate'). process: map to washed/natural/honey/anaerobic, else other."
)
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "origin_country",
        "origin_region",
        "producer",
        "process",
        "varietals",
        "elevation_masl_min",
        "elevation_masl_max",
        "roast_level",
        "tasting_notes",
        "is_blend",
        "is_decaf",
    ],
    "properties": {
        "origin_country": {"type": ["string", "null"]},
        "origin_region": {"type": ["string", "null"]},
        "producer": {"type": ["string", "null"]},
        "process": {
            "anyOf": [
                {"type": "string", "enum": ["washed", "natural", "honey", "anaerobic", "other"]},
                {"type": "null"},
            ]
        },
        "varietals": {"type": "array", "items": {"type": "string"}},
        "elevation_masl_min": {"type": ["integer", "null"]},
        "elevation_masl_max": {"type": ["integer", "null"]},
        "roast_level": {"anyOf": [{"type": "string", "enum": ["light", "medium", "dark"]}, {"type": "null"}]},
        "tasting_notes": {"type": "array", "items": {"type": "string"}},
        "is_blend": {"type": ["boolean", "null"]},
        "is_decaf": {"type": ["boolean", "null"]},
    },
}


def llm_request(title: str, tags: list[str], body_text: str) -> dict:
    listing = f"Title: {title}\nTags: {', '.join(tags)}\n\n{body_text[:4000]}"
    return {
        "model": MODEL,
        "max_tokens": 1024,
        "system": SYSTEM,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": listing}],
    }


def parse_llm_message(msg: object, base: Enrichment) -> Enrichment | None:
    if getattr(msg, "stop_reason", None) in ("refusal", "max_tokens"):
        return None
    text = next((b.text for b in msg.content if b.type == "text"), "")  # type: ignore[attr-defined]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    merged = base.model_dump() | {k: v for k, v in data.items() if v not in (None, [])}
    if merged.get("origin_country"):
        merged["origin_country"] = canonical_country(merged["origin_country"])
    merged["tasting_notes"] = split_notes(",".join(merged.get("tasting_notes") or []))
    merged["method"] = "llm"
    try:
        return Enrichment(**merged)
    except ValueError:
        return None


# Claude Code cloud sessions don't pass ANTHROPIC_API_KEY through (it would take over the session's own model
# access), so the pipeline also reads its key under its own name. GitHub Actions sets ANTHROPIC_API_KEY.
KEY_VAR = "COFFEEDATA_ANTHROPIC_API_KEY"


def llm_enabled() -> bool:
    return bool(
        os.environ.get(KEY_VAR)
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def client():
    """An Anthropic client from COFFEEDATA_ANTHROPIC_API_KEY if set, else the SDK's own credential lookup."""
    import anthropic

    key = os.environ.get(KEY_VAR)
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
