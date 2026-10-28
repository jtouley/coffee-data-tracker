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
# Spelled with or without the separator: "SL-28", "SL 28", "SL28"; "Ruiru11", "Ruiru 11".
_VARIETAL_SPELLINGS = {"SL28": r"SL[- ]?28", "SL34": r"SL[- ]?34", "Ruiru 11": r"Ruiru[- ]?11"}
_VARIETAL = re.compile(
    r"\b(" + "|".join(_VARIETAL_SPELLINGS.get(v, re.escape(v)) for v in VARIETALS) + r")\b", re.I
)
_VARIETAL_NAME = {re.sub(r"[\s-]", "", v.lower()): v for v in VARIETALS}


def _varietal_name(s: str) -> str:
    return _VARIETAL_NAME.get(re.sub(r"[\s-]", "", s.lower()), s.title())


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


def _strip_false_places(text: str) -> str:
    """Blank out (keeping offsets) names that look like an origin but aren't: "java" unless the text names a Javanese
    place, and the Flores in Spanish place names ("Las Flores, Honduras"), which isn't the Indonesian island."""
    if not _JAVA_ORIGIN.search(text):
        text = re.sub(r"\bjava\b", lambda m: " " * len(m.group()), text, flags=re.I)
    return re.sub(r"\b((?:las|los|de)\s+)flores\b", lambda m: m.group(1) + " " * 6, text, flags=re.I)


def _countries(text: str) -> list[str]:
    text = _strip_false_places(text)
    found = [canonical_country(m.group(1)) for m in _COUNTRY.finditer(text)]
    if not found and (m := _DEMONYM.search(text)):
        found = [DEMONYMS[m.group(1).lower()]]
    return list(dict.fromkeys(found))


def _country(text: str) -> str | None:
    found = _countries(text)
    return found[0] if found else None


# --- Origin by context ------------------------------------------------------------------------------
# A listing names countries for reasons other than where the coffee grew: where its variety was bred ("SL-28 is a
# variety developed in Kenya"), a variety named after one ("Variedad Colombia"), or another coffee ("brighter than
# most coffees from Brazil"). Each mention of a country, demonym or one-country growing region gets a kind from its
# context, and the best-ranked kind decides (see _RANK).
_REGION = re.compile(
    r"\b(" + "|".join(re.escape(r) for r in sorted(REGIONS, key=len, reverse=True)) + r")\b"
    # Antigua and Barbuda grows no coffee to speak of; Santa Barbara, California is a roaster's home, not a farm's.
    r"(?!\s*(?:and|&)\s+barbuda|,?\s+(?:ca|california)\b)",
    re.I,
)
# "Finca Santa Barbara" is a farm's name, which says nothing about where the farm is.
_FARM_NAME = re.compile(r"\b(?:finca|hacienda|fazenda|s[ií]tio|beneficio|estate|farm)\s+$", re.I)
_VARIETY_WORD = rf"(?:{_VARIETAL.pattern}|\bvariet(?:y|al|ies|als)\b|\bcultivars?\b|\blandraces?\b)"
# Where a variety comes from, with the variety as the subject: "SL-28 is a variety developed in Kenya", "Gesha,
# originally from Ethiopia", "SL-28, bred by Scott Laboratories in Kenya". Not "grown by indigenous farmers in
# Guatemala" or "we first discovered this Bourbon lot in Rwanda": the verb must follow the variety, and the
# preposition the verb.
_BRED = re.compile(
    _VARIETY_WORD + r"[^.;!?\n]{0,60}?\b(?:developed|bred|native|indigenous|originally|originat(?:ed|es|ing)|"
    r"discovered|descend(?:s|ed|ing|ants?)?)\b(?:\s+by(?:\s+[\w'’.-]+){1,3}?)?\s+(?:in|to|from|of)\s+"
    r"(?:the\s+)?(?:[\w'’-]+\s+){0,2}$",
    re.I,
)
# A country that is a variety's name: "Variedad Colombia", "var. Colombia". ("Colombia variety" stays attributive:
# listings say "Ethiopia heirloom varieties" of their own origin.)
_VARIETY_NAMED = re.compile(r"\b(?:variedad|variety|varietal|var\.|cultivar)\s+$", re.I)
# "Kenya SL-28", "Kenyan SL-28", "Ethiopia Landrace Variety", "Ethiopia heirloom": a country describing the variety.
_ATTRIBUTIVE = re.compile(rf"^[\s-]*(?:{_VARIETY_WORD}|bred\b|developed\b)", re.I)
# "Origin: Honduras", "Region: Marcala, Honduras", "Country of origin - Kenyan"
_LABEL = re.compile(
    r"\b(?:origin|country(?: of origin)?|region|location)\s*[:\-–]\s*(?:[^\n,:;]{1,40},\s*)?$", re.I
)
# "Marcala, Honduras" (a known region), or "in Las Flores, Honduras" (a place name after in/at/from/near). Not
# "Today, Kenya ...".
_REGION_COMMA = re.compile(_REGION.pattern + r",\s*$", re.I)
_PLACE_COMMA = re.compile(
    r"\b(?:in|at|from|near)\s+([A-Z][\w'’.-]*(?:\s+(?:de\s+|del\s+)?[A-Z][\w'’.-]*){0,3}),\s*$"
)
# "a farm in Honduras", "grown by indigenous farmers in Guatemala", "cultivated in Honduras"
_LOCATED = re.compile(
    r"\b(?:farms?|fincas?|estates?|stations?|cooperatives?|co-?ops?|located|grow(?:n|s|ing)?|produced|cultivat\w*|"
    r"harvested|sourced)\b[^.;!?\n]{0,50}?\b(?:in|at|from)\s+(?:the\s+)?(?:[\w'’-]+\s+){0,2}$",
    re.I,
)
ORIGIN, PLAIN, ATTR, BRED = "origin", "plain", "attributive", "bred"
# (where, what, kind) -> rank; the best (lowest) rank present wins, then the most-named country, then the first.
# A country the title names outright wins, as before; one the title uses for a variety ("Kenya SL-28") yields to a
# description that names the origin. Body mentions other than an origin count only in the first 1500 characters.
# BRED mentions never count, and a passing mention of a country the text says a variety was bred in ("Kenya
# remains its best-known home") counts only after every other kind.
_RANK = {
    ("title", "country", ORIGIN): 0,
    ("title", "demonym", ORIGIN): 0,
    ("title", "country", PLAIN): 1,
    ("title", "demonym", PLAIN): 2,
    ("title", "region", PLAIN): 3,
    ("body", "country", ORIGIN): 4,
    ("body", "demonym", ORIGIN): 4,
    ("title", "country", ATTR): 5,
    ("title", "demonym", ATTR): 5,
    ("body", "country", PLAIN): 5,
    ("body", "country", ATTR): 5,
    ("body", "demonym", PLAIN): 6,
    ("body", "demonym", ATTR): 6,
    ("body", "region", PLAIN): 7,
}
_BRED_COUNTRY_RANK = 8


def _kind(text: str, start: int, end: int) -> str:
    """How the country or demonym at text[start:end] relates to the coffee."""
    before = re.split(r"[.!?;\n]", text[max(0, start - 120) : start])[-1]
    after = re.split(r"[.!?;\n]", text[end : end + 40])[0]
    if _BRED.search(before) or _VARIETY_NAMED.search(before):
        return BRED
    if _LABEL.search(before) or _REGION_COMMA.search(before) or _LOCATED.search(before):
        return ORIGIN
    if (m := _PLACE_COMMA.search(before)) and not (
        _COUNTRY.search(m.group(1)) or _DEMONYM.search(m.group(1))
    ):
        return ORIGIN
    return ATTR if _ATTRIBUTIVE.search(after) else PLAIN


Mention = tuple[str, str, str, str, int]  # (where, what, kind, country, position)


def _mentions(where: str, text: str) -> list[Mention]:
    text = _strip_false_places(text)
    out: list[Mention] = []
    for what, pat, name in (
        ("country", _COUNTRY, canonical_country),
        ("demonym", _DEMONYM, lambda s: DEMONYMS[s.lower()]),
    ):
        for m in pat.finditer(text):
            out.append((where, what, _kind(text, m.start(), m.end()), name(m.group(1)), m.start()))
    for m in _REGION.finditer(text):
        if not _FARM_NAME.search(text[max(0, m.start() - 20) : m.start()]):
            out.append((where, "region", PLAIN, REGIONS[m.group(1).lower()], m.start()))
    return out


def _title_countries(mentions: list[Mention]) -> list[str]:
    """The countries a title names (its demonym if it names none), not counting variety names."""
    found = [(what, c) for where, what, kind, c, _ in mentions if where == "title" and kind != BRED]
    names = [c for what, c in found if what == "country"] or [c for what, c in found if what == "demonym"][:1]
    return list(dict.fromkeys(names))


def origin_country(mentions: list[Mention]) -> str | None:
    """The origin the title and description name, by the best-ranked kind of mention (see _RANK)."""
    bred = {c for _, _, kind, c, _ in mentions if kind == BRED}
    ranked: list[tuple[int, tuple[int, int], str]] = []
    for where, what, kind, c, pos in mentions:
        rank = _RANK.get((where, what, kind))
        if rank is None or (where == "body" and kind != ORIGIN and pos >= 1500):
            continue
        if where == "body" and kind != ORIGIN and c in bred:
            rank = _BRED_COUNTRY_RANK
        ranked.append((rank, (0 if where == "title" else 1, pos), c))
    if not ranked:
        return None
    best = min(r for r, _, _ in ranked)
    names = [c for r, _, c in sorted(ranked, key=lambda t: t[1]) if r == best]
    counts = Counter(names)
    return max(dict.fromkeys(names), key=lambda n: counts[n])


def _region(text: str) -> str | None:
    m = _REGION.search(text)
    return REGIONS[m.group(1).lower()] if m else None


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
    mentions = _mentions("title", item.title) + _mentions("body", body)
    in_title = _title_countries(mentions)
    no_origin = len(in_title) > 1 or bool(_NO_ORIGIN.search(item.title))
    e.origin_country = (
        _country(origin_tag) or _region(origin_tag) or (None if no_origin else origin_country(mentions))
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
            _varietal_name(v.group(1))
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
        e.origin_country = e.origin_country if in_title else None
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
