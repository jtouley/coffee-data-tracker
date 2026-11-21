"""Structured coffee attributes: deterministic rules first, Claude Haiku (optional) for what rules miss."""

from __future__ import annotations

import json
import logging
import os
import re

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


# Names that look like an origin but aren't: the Flores in Spanish place names ("Las Flores, Honduras") isn't the
# Indonesian island, New Mexico isn't Mexico, a Brazil nut is a tasting note, and Colombia is also a variety
# ("Variedad Colombia", "var. Colombia").
_FALSE_PLACES = re.compile(
    r"\b(?:(?:las|los|de)\s+(flores)|new\s+(mexico)|(brazil)\s+nuts?|(?:variedad|var\.?|variety|varietal|cultivar)\s+"
    r"(colombia)|(colombia)\s+(?:variet(?:y|al|ies)|cultivar))\b",
    re.I,
)


def _strip_false_places(text: str) -> str:
    """Blank out (keeping offsets) "java" unless the text names a Javanese place, and the names in _FALSE_PLACES."""
    if not _JAVA_ORIGIN.search(text):
        text = re.sub(r"\bjava\b", lambda m: " " * len(m.group()), text, flags=re.I)

    def blank(m: re.Match[str]) -> str:
        g = next(i for i in range(1, 6) if m.group(i))
        s, e = m.start(g) - m.start(), m.end(g) - m.start()
        return m.group()[:s] + " " * (e - s) + m.group()[e:]

    return _FALSE_PLACES.sub(blank, text)


def _countries(text: str) -> list[str]:
    """The countries a text names, in order (its first demonym if it names none)."""
    text = _strip_false_places(text)
    found = [canonical_country(m.group(1)) for m in _COUNTRY.finditer(text)]
    if not found and (m := _DEMONYM.search(text)):
        found = [DEMONYMS[m.group(1).lower()]]
    return list(dict.fromkeys(found))


def _country(text: str) -> str | None:
    found = _countries(text)
    return found[0] if found else None


# --- Origin -----------------------------------------------------------------------------------------
# The rules only answer when the listing is clear. When it isn't, they leave the origin empty, and needs_llm()
# sends the product to Claude, which reads the description in context.
_REGION = re.compile(
    r"\b(" + "|".join(re.escape(r) for r in sorted(REGIONS, key=len, reverse=True)) + r")\b"
    # Antigua and Barbuda grows no coffee to speak of; Santa Barbara, California is a roaster's home, not a farm's.
    r"(?!\s*(?:and|&)\s+barbuda|,?\s+(?:ca|california)\b)",
    re.I,
)
# "Finca Santa Barbara" is a farm's name, which says nothing about where the farm is.
_FARM_NAME = re.compile(r"\b(?:finca|hacienda|fazenda|s[ií]tio|beneficio|estate|farm)\s+$", re.I)
# A labelled origin field: "Origin: Honduras", "ORIGIN | Ethiopia & Colombia", "Region: Marcala, Honduras". Every
# country in the field counts, so "Origin: Brazil/Honduras" names two. The field ends at the line, the sentence or
# the next "Label:".
_LABEL_FIELD = re.compile(
    r"\b(?:origins?|country(?: of origin)?|region|location)\s*[:|\-–]\s*(.{1,100}?)(?=\n|\.\s|;|\s[A-Z][\w ]{1,20}:|$)",
    re.I,
)
# "Marcala, Honduras": a region followed by its own country (not "Chiapas, Brazil", a list). "in Las Flores,
# Honduras": a place after in/at/from.
_REGION_COMMA = re.compile(_REGION.pattern + r",\s*$", re.I)
_PLACE_COMMA = re.compile(
    r"\b(?:in|at|from)\s+([A-Z][\w'’.-]*(?:\s+(?:de\s+|del\s+)?[A-Z][\w'’.-]*){0,3}),\s*$"
)
# ...unless the country goes on into a list of places ("customers in New York, Puerto Rico, and El Paso"): then the
# place and the country are two items of the list, not a place and its country.
# Anything after the country that can continue a list (a comma, "&", "and", in any case) counts. It was chosen over
# also requiring a capitalised next place on eval/origin's validation set, after a targeted relabel there
# (eval/origin/README.md), so validation is not a clean measure of it; the next sealed test set is. A place and its
# country followed by a comma ("in San Pedro, Guatemala, at 1,600 m") then falls back to the countries the
# description names.
_LIST_GOES_ON = re.compile(r"\s*(?:,|&|\band\b)", re.I)
# A title country that is part of a variety's name: "Kenya SL-28", "Kenyan SL-28", "Ethiopia Landrace".
_VARIETY_NEXT = re.compile(
    rf"^[\s-]*(?:{_VARIETAL.pattern}|\bvariet(?:y|al)\b|\bcultivar\b|\blandrace\b)", re.I
)


def _named(text: str) -> list[str]:
    """Countries and demonyms in `text`, as countries, in order."""
    found = [(m.start(), canonical_country(m.group(1))) for m in _COUNTRY.finditer(text)]
    found += [(m.start(), DEMONYMS[m.group(1).lower()]) for m in _DEMONYM.finditer(text)]
    return [c for _, c in sorted(found)]


def _stated_origins(body: str) -> list[str]:
    """Countries the description gives as its origin, as opposed to mentioning them."""
    text = _strip_false_places(body)
    out = [c for m in _LABEL_FIELD.finditer(text) for c in _named(m.group(1))]
    for m in _COUNTRY.finditer(text):
        country = canonical_country(m.group(1))
        before = text[max(0, m.start() - 80) : m.start()].rsplit("\n", 1)[-1]
        region = _REGION_COMMA.search(before)
        place = _PLACE_COMMA.search(before)
        in_list = bool(_LIST_GOES_ON.match(text, m.end()))
        if (region and REGIONS[region.group(1).lower()] == country) or (
            place and not _named(place.group(1)) and not in_list
        ):
            out.append(country)
    return list(dict.fromkeys(out))


def _regions(text: str) -> list[str]:
    """Countries of the one-country growing regions a text names, leaving out farms named after one."""
    return list(
        dict.fromkeys(
            REGIONS[m.group(1).lower()]
            for m in _REGION.finditer(text)
            if not _FARM_NAME.search(text[max(0, m.start() - 20) : m.start()])
        )
    )


def _title_is_variety(title: str, country: str) -> bool:
    """Whether every mention of `country` in the title is part of a variety's name ("Kenya SL-28")."""
    text = _strip_false_places(title)
    hits = [
        m
        for pat, name in ((_COUNTRY, canonical_country), (_DEMONYM, lambda s: DEMONYMS[s.lower()]))
        for m in pat.finditer(text)
        if name(m.group(1)) == country
    ]
    return bool(hits) and all(_VARIETY_NEXT.search(text[m.end() :]) for m in hits)


def _one(found: list[str]) -> str | None:
    return found[0] if len(found) == 1 else None


def origin_country(title: str, in_title: list[str], body: str, tag_region: str = "") -> str | None:
    """Where the coffee grew, or None when the listing doesn't say clearly.

    A country in the title wins, unless it is part of a variety's name ("Kenya SL-28") and the description states a
    different origin or region. Otherwise the description's stated origin ("Origin:", "Marcala, Honduras"), else the
    one country the top of the description names. Several candidates is no answer. A growing region only counts when
    no country is named anywhere, and a region that disagrees with the description's one country is no answer either.
    """
    stated = _stated_origins(body)
    regions = list(dict.fromkeys(_regions(title) + _regions(tag_region) + _regions(body)))
    if in_title:
        c = in_title[0]
        if _title_is_variety(title, c) and set(stated + regions) - {c}:
            return None
        return c
    if stated:
        return _one(stated)
    if named := _countries(body[:1500]):
        return _one(named) if not set(regions) - set(named) else None
    if _countries(body):
        return None
    return _one(regions)


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
    in_title = _countries(item.title)
    # A title naming several countries ("Puerto Rico Supremo, Mexico, or Colombia") is a choice or a blend.
    no_origin = len(in_title) > 1 or bool(_NO_ORIGIN.search(item.title))
    e.origin_country = _country(origin_tag) or (
        None if no_origin else origin_country(item.title, in_title, body, tag_region=origin_tag)
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


# A word that suggests the listing says how the coffee was processed where the rules could not read it. Generous on
# purpose (eval/process, #252: tuned for recall on labelled train listings): it only orders Claude's weekly queue.
PROCESS_HINT = re.compile(
    r"wash|natural|honey|anaerob|ferment|pulp|dried|drying|process|macerat|hull|giling|semi[- ]?wash|monsoon|"
    r"carbonic|lavado|miel|mucilage|cherr(?:y|ies)\s+(?:were|are|is)|raised\s+bed|patio",
    re.I,
)
# Decaffeination is not a process ("Swiss Water® Process", "natural decaf process", "Sugarcane EA process"): left out
# of the title, tags and description before looking for a hint.
_DECAF_TEXT = re.compile(
    r"\b(?:(?:swiss|mountain|mtn\.?)\s+water(?:[®™\s]+(?:decaf\w*\s+)?process\w*)?|water[- ]process\w*|"
    r"sugar[- ]?cane(?:\s+(?:e\.?a\.?|ethyl[- ]acetate|decaf\w*))?(?:\s+process\w*)?|co2(?:\s+process\w*)?|"
    r"ethyl[- ]acetate(?:\s+process\w*)?|e\.?a\.?(?:\s+process\w*)\b|"
    r"natural(?:ly)?\s+decaf\w*(?:\s+process\w*)?|decaf\w*\s+process\w*)",
    re.I,
)


def process_first(item: CatalogItem, e: Enrichment) -> bool:
    """Whether Claude should read this coffee ahead of the rest of the weekly queue: a single origin whose process
    the rules left empty, and whose title, tags or description has a word that suggests it states one."""
    if e.process or e.is_blend:
        return False
    text = _DECAF_TEXT.sub(" ", f"{item.title} {' '.join(item.tags)} {item.body_text[:4000]}")
    return bool(PROCESS_HINT.search(text))


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


# The longest value kept from Claude's answer: a text field, a list, and one item of a list. A listing is the roaster's
# own text, so its answer is held to the schema's keys and to these sizes (security review 2026-11-21, F3). The output
# schema can't carry them (structured outputs don't accept maxLength or maxItems), so they are checked here. They are
# above every value enriched so far (the longest: a producer of 128 characters, 8 notes or varietals, an item of 29).
MAX_TEXT_CHARS = 200
MAX_LIST_ITEMS = 20
MAX_ITEM_CHARS = 80
_ANSWER_KEYS: tuple[str, ...] = tuple(SCHEMA["properties"])  # type: ignore[arg-type]


# The enrichments table's integer columns are int32: a value outside it would fail the batch's write (Lake.write).
_INT32 = (-(2**31), 2**31 - 1)


class _WrongType(ValueError):
    """A value of a type the schema doesn't allow (or out of an int32 column's range): the answer is not used."""


def _schema_types(spec: dict) -> set[str]:
    """The JSON types a SCHEMA property allows ("anyOf" of an enum and null included)."""
    types: set[str] = set()
    for part in spec.get("anyOf", [spec]):
        t = part.get("type")
        types |= set(t) if isinstance(t, list) else {t}
    return types


def _check_type(key: str, v: object) -> None:
    spec = SCHEMA["properties"][key]  # type: ignore[index]
    allowed = _schema_types(spec)
    if isinstance(v, bool):
        ok = "boolean" in allowed
    elif isinstance(v, int):
        ok = "integer" in allowed and _INT32[0] <= v <= _INT32[1]
    elif isinstance(v, str):
        ok = "string" in allowed
    elif isinstance(v, list):
        ok = "array" in allowed and all(isinstance(x, str) for x in v)
    else:
        ok = False  # a float, an object: no field takes one
    if not ok:
        raise _WrongType(key)


def _answer_fields(data: dict) -> dict:
    """The answer's fields that SCHEMA names, each of its schema's type (else _WrongType) and within the size limits:
    an over-long text is dropped (left to the rules' value), a list keeps its first MAX_LIST_ITEMS strings within
    MAX_ITEM_CHARS, and an integer must fit the int32 column. A key outside the schema (body_hash, method) is
    ignored."""
    out: dict = {}
    for k in _ANSWER_KEYS:
        v = data.get(k)
        if v is None or v == []:
            continue
        _check_type(k, v)
        if isinstance(v, list):
            items = [x for x in v if len(x) <= MAX_ITEM_CHARS]
            if items:
                out[k] = items[:MAX_LIST_ITEMS]
        elif isinstance(v, str):
            if len(v) <= MAX_TEXT_CHARS:
                out[k] = v
        else:
            out[k] = v
    return out


def parse_llm_message(msg: object, base: Enrichment) -> Enrichment | None:
    """Claude's answer merged over the rules' enrichment, or None when it gave none: a refusal, a cut-off answer, text
    that isn't JSON, JSON that isn't an object (`[]` used to raise here and stop the whole batch), or a value of the
    wrong type or out of its column's range."""
    if getattr(msg, "stop_reason", None) in ("refusal", "max_tokens"):
        return None
    text = next((b.text for b in msg.content if b.type == "text"), "")  # type: ignore[attr-defined]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        fields = _answer_fields(data)
    except (
        _WrongType
    ):  # a wrongly typed or out-of-range value: the whole answer is left out, the rules' value stays
        return None
    merged = base.model_dump() | fields
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
