"""Structured coffee attributes: deterministic rules first, Claude Haiku (optional) for what rules miss."""

from __future__ import annotations

import json
import logging
import os
import re

from .models import CatalogItem, Enrichment
from .origins import COUNTRIES, canonical_country

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


def _countries(text: str) -> list[str]:
    if not _JAVA_ORIGIN.search(text):
        text = re.sub(r"\bjava\b", " ", text, flags=re.I)
    found = [canonical_country(m.group(1)) for m in _COUNTRY.finditer(text)]
    if not found and (m := _DEMONYM.search(text)):
        found = [DEMONYMS[m.group(1).lower()]]
    return list(dict.fromkeys(found))


def _country(text: str) -> str | None:
    found = _countries(text)
    return found[0] if found else None


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
    e.origin_country = _country(origin_tag) or (
        None if no_origin else _country(item.title) or _country(body[:1500])
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
