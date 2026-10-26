"""The personal shot log (data/shots.jsonl): logging, catalog matching, and dial-in advice."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from .models import Shot

SHOTS = Path("data/shots.jsonl")
CATALOG = Path("data/catalog")
REGISTRY = Path("config/roasters.csv")

TARGET_RATIO = (1.8, 2.3)
TARGET_TIME = (25.0, 32.0)

# Loose input keys people actually type -> Shot fields.
ALIASES = {
    "in": "dose_g", "dose": "dose_g", "out": "yield_g", "yield": "yield_g", "time": "time_s",
    "seconds": "time_s", "grind": "grinder_setting", "grinder": "grinder_setting", "temp": "water_temp_c",
    "water_temp": "water_temp_c", "method": "brew_method", "milk": "is_milk_drink", "coffee": "coffee_freetext",
    "bean": "coffee_freetext",
}


# --- catalog matching --------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _index(catalog: Path = CATALOG, registry: Path = REGISTRY) -> dict[str, str]:
    """coffee_id -> 'Roaster Name Coffee Title' for fuzzy search."""
    import csv

    names = {}
    if registry.exists():
        with registry.open(newline="") as f:
            names = {r["slug"]: r["name"] for r in csv.DictReader(f)}
    idx = {}
    for p in sorted(catalog.glob("*.json")) if catalog.exists() else []:
        for item in json.loads(p.read_text()):
            idx[f"{item['roaster_slug']}:{item['handle']}"] = f"{names.get(item['roaster_slug'], '')} {item['title']}"
    return idx


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1 and t not in {"coffee", "roasters", "co"}]


def match_score(query: str, label: str) -> int:
    """Mostly: what share of the query's words appear (fuzzily) in the label; WRatio breaks ties."""
    q, lab = _tokens(query), _tokens(label)
    if not q or not lab:
        return 0
    covered = sum(any(fuzz.ratio(a, b) >= 85 for b in lab) for a in q) / len(q)
    return round(70 * covered + 0.3 * fuzz.WRatio(query, label))


def fuzzy_match_coffee(text: str, limit: int = 3) -> list[dict]:
    idx = _index()
    if not text or not idx:
        return []
    pool = process.extract(text, idx, scorer=fuzz.partial_token_set_ratio, limit=300)
    scored = sorted(((match_score(text, label), key, label) for label, _, key in pool), reverse=True)
    return [{"coffee_id": key, "label": label.strip(), "score": score} for score, key, label in scored[:limit]]


# --- logging -----------------------------------------------------------------------------------------

def normalize(payload: dict) -> dict:
    out: dict = {}
    for k, v in payload.items():
        key = ALIASES.get(k.strip().lower().replace(" ", "_"), k.strip().lower().replace(" ", "_"))
        if isinstance(v, str):
            v = v.strip()
            if v in ("", "_No response_", "None"):
                continue
            if key in ("dose_g", "yield_g", "time_s", "grinder_setting", "water_temp_c"):
                v = re.sub(r"[^\d.]", "", v) or None
            elif key == "is_milk_drink":
                v = v.lower() in ("true", "yes", "y", "1", "x", "on", "milk")
        out[key] = v
    return out


def log_shot(payload: dict, path: Path = SHOTS, min_score: int = 80) -> tuple[Shot, list[dict]]:
    """Validate and append a shot. Free-text coffee is matched against the catalog; returns (shot, matches)."""
    data = normalize(payload)
    data.setdefault("shot_id", str(uuid.uuid4()))
    data.setdefault("brewed_at", datetime.now(UTC).isoformat(timespec="seconds"))
    matches: list[dict] = []
    if not data.get("coffee_id") and data.get("coffee_freetext"):
        matches = fuzzy_match_coffee(data["coffee_freetext"])
        clear_winner = len(matches) < 2 or matches[0]["score"] - matches[1]["score"] >= 5
        if matches and matches[0]["score"] >= min_score and clear_winner:
            data["coffee_id"] = matches[0]["coffee_id"]
    shot = Shot(**data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(shot.model_dump_json(exclude_none=True) + "\n")
    return shot, matches


def load_shots(path: Path = SHOTS) -> list[Shot]:
    if not path.exists():
        return []
    return [Shot(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def history(ref: str, path: Path = SHOTS) -> list[Shot]:
    """Shots for a coffee_id, or whose free-text coffee resembles ref."""
    shots = load_shots(path)
    exact = [s for s in shots if s.coffee_id == ref]
    if exact:
        return sorted(exact, key=lambda s: s.brewed_at)
    return sorted((s for s in shots if s.coffee_freetext and fuzz.WRatio(ref, s.coffee_freetext) >= 85),
                  key=lambda s: s.brewed_at)


# --- dial-in advice ----------------------------------------------------------------------------------

def _step(shots: list[Shot]) -> float:
    settings = sorted({s.grinder_setting for s in shots})
    diffs = [b - a for a, b in zip(settings, settings[1:], strict=False) if b > a]
    return min(diffs) if diffs else 1.0


def suggest_next(ref: str, path: Path = SHOTS, finer_is_lower: bool = True) -> str:
    """Deterministic espresso dial-in advice from the last shot of this coffee (target ~1:2 in 25-32 s)."""
    shots = history(ref, path)
    if not shots:
        return "No shots logged for this coffee yet. Start around 1:2 (e.g. 18 g in, 36 g out) in 25–32 s."
    last = shots[-1]
    ratio = last.yield_g / last.dose_g
    step = _step(shots)
    finer = last.grinder_setting - step if finer_is_lower else last.grinder_setting + step
    coarser = last.grinder_setting + step if finer_is_lower else last.grinder_setting - step
    desc = f"Last shot: {last.dose_g:g} g → {last.yield_g:g} g (1:{ratio:.1f}) in {last.time_s:g} s at grind {last.grinder_setting:g}"
    if last.taste == "sour" or (last.taste is None and last.time_s < TARGET_TIME[0]):
        why = "tasted sour" if last.taste == "sour" else f"ran fast ({last.time_s:g} s)"
        advice = f"It {why}: go finer to {finer:g} to slow the shot and extract more."
    elif last.taste == "bitter" or (last.taste is None and last.time_s > TARGET_TIME[1]):
        why = "tasted bitter" if last.taste == "bitter" else f"ran slow ({last.time_s:g} s)"
        advice = f"It {why}: go coarser to {coarser:g}."
    elif not TARGET_RATIO[0] <= ratio <= TARGET_RATIO[1]:
        target = round(last.dose_g * 2)
        advice = f"Timing is fine; pull to about {target} g out to get closer to 1:2."
    else:
        advice = "That's in the window. Keep the recipe and adjust by taste."
    best = max(shots, key=lambda s: (s.rating, s.brewed_at))
    best_s = (f" Your best-rated shot of this coffee ({best.rating}/5): grind {best.grinder_setting:g}, "
              f"{best.dose_g:g} g → {best.yield_g:g} g in {best.time_s:g} s.") if len(shots) > 1 else ""
    return f"{desc}. {advice}{best_s}"


# --- GitHub issue form ------------------------------------------------------------------------------

ISSUE_FIELDS = {
    "coffee": "coffee_freetext", "grind setting": "grinder_setting", "dose (g)": "dose_g",
    "yield (g)": "yield_g", "time (s)": "time_s", "rating": "rating", "taste": "taste",
    "water temp (°c)": "water_temp_c", "roast date": "roast_date", "brew method": "brew_method",
    "notes": "notes",
}


def parse_issue_body(body: str) -> dict:
    """Parse a GitHub issue-form body ('### Label' sections) into shot fields."""
    out: dict = {}
    for m in re.finditer(r"^###\s+(.+?)\s*\n(.*?)(?=^###\s|\Z)", body, re.S | re.M):
        label, value = m.group(1).strip().lower(), m.group(2).strip()
        if label == "milk drink":
            out["is_milk_drink"] = bool(re.search(r"-\s*\[x\]", value, re.I))
            continue
        key = ISSUE_FIELDS.get(label)
        if key and value and value != "_No response_":
            out[key] = value.split(" ")[0] if key == "rating" else value
    return out


def issue_reply(shot: Shot, matches: list[dict], path: Path = SHOTS) -> str:
    lines = [f"Logged shot `{shot.shot_id}`."]
    if shot.coffee_id:
        lines.append(f"Matched coffee: **{matches[0]['label'] if matches else shot.coffee_id}** (`{shot.coffee_id}`).")
    elif matches:
        opts = ", ".join(f"`{m['coffee_id']}` ({m['score']})" for m in matches)
        lines.append(f"No confident catalog match for “{shot.coffee_freetext}”. Closest: {opts}.")
    lines.append("")
    lines.append(suggest_next(shot.coffee_id or shot.coffee_freetext or "", path))
    return "\n".join(lines)
