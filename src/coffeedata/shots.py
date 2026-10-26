"""The personal shot log (data/shots.jsonl): logging, catalog matching, and dial-in advice."""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import UTC, datetime

from rapidfuzz import fuzz, process

from .lake import Lake
from .models import Shot

log = logging.getLogger(__name__)

INDEX_TTL_S = 3600

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

_index_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _index(lake: Lake | None = None) -> dict[str, str]:
    """coffee_id -> 'Roaster Name Coffee Title', from the weekly dim_coffee export; refreshed hourly so a
    long-running MCP server picks up new catalog weeks."""
    lake = lake or Lake()
    cached = _index_cache.get(lake.url)
    if cached and time.monotonic() - cached[0] < INDEX_TTL_S:
        return cached[1]
    idx: dict[str, str] = {}
    try:
        con = lake.connect()
        src = ", ".join(f"'{s}'" for s in lake.mart_sources("dim_coffee"))
        for cid, roaster, title in con.execute(
                f"select coffee_id, roaster_name, title from read_parquet([{src}], union_by_name = true)").fetchall():
            idx[cid] = f"{roaster or ''} {title or ''}"
    except Exception as e:  # noqa: BLE001 - no catalog yet just means no matches
        log.info("no catalog index: %s", e)
    _index_cache[lake.url] = (time.monotonic(), idx)
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


def fuzzy_match_coffee(text: str, limit: int = 3, lake: Lake | None = None) -> list[dict]:
    idx = _index(lake)
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


def log_shot(payload: dict, lake: Lake | None = None, min_score: int = 80) -> tuple[Shot, list[dict]]:
    """Validate and record a shot as its own immutable lake file. Free-text coffee is matched to the catalog.
    To correct a shot, log it again with the same shot_id (latest wins); add deleted=True to retract it."""
    lake = lake or Lake()
    data = normalize(payload)
    deleted = bool(data.pop("deleted", False))
    data.setdefault("shot_id", str(uuid.uuid4()))
    data.setdefault("brewed_at", datetime.now(UTC).isoformat(timespec="seconds"))
    matches: list[dict] = []
    if not data.get("coffee_id") and data.get("coffee_freetext"):
        matches = fuzzy_match_coffee(data["coffee_freetext"], lake=lake)
        clear_winner = len(matches) < 2 or matches[0]["score"] - matches[1]["score"] >= 5
        if matches and matches[0]["score"] >= min_score and clear_winner:
            data["coffee_id"] = matches[0]["coffee_id"]
    shot = Shot(**data)
    now = datetime.now(UTC).replace(microsecond=0)
    lake.write("shots", f"shots/{shot.shot_id}__{now:%Y%m%dT%H%M%S}.parquet",
               [shot.model_dump() | {"logged_at": now, "deleted": deleted}])
    return shot, matches


def load_shots(lake: Lake | None = None) -> list[Shot]:
    """Current shots: the latest record per shot_id, without retracted ones."""
    lake = lake or Lake()
    cur = lake.connect().execute(f"""
        select * exclude (logged_at, deleted) from {lake.scan('shots')}
        where shot_id is not null
        qualify row_number() over (partition by shot_id order by logged_at desc) = 1 and not deleted""")
    cols = [d[0] for d in cur.description]
    return [Shot(**{k: v for k, v in zip(cols, r, strict=True) if v is not None}) for r in cur.fetchall()]


def history(ref: str, lake: Lake | None = None) -> list[Shot]:
    """Shots for a coffee_id, or whose free-text coffee resembles ref."""
    shots = load_shots(lake)
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


def suggest_next(ref: str, lake: Lake | None = None, finer_is_lower: bool = True) -> str:
    """Deterministic espresso dial-in advice from the last shot of this coffee (target ~1:2 in 25-32 s)."""
    shots = history(ref, lake)
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


def issue_reply(shot: Shot, matches: list[dict], lake: Lake | None = None) -> str:
    lines = [f"Logged shot `{shot.shot_id}`."]
    if shot.coffee_id:
        lines.append(f"Matched coffee: **{matches[0]['label'] if matches else shot.coffee_id}** (`{shot.coffee_id}`).")
    elif matches:
        opts = ", ".join(f"`{m['coffee_id']}` ({m['score']})" for m in matches)
        lines.append(f"No confident catalog match for “{shot.coffee_freetext}”. Closest: {opts}.")
    lines.append("")
    lines.append(suggest_next(shot.coffee_id or shot.coffee_freetext or "", lake))
    return "\n".join(lines)
