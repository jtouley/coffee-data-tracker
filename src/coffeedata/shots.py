"""The personal shot log (shots/ in the lake): logging, catalog matching, and dial-in advice."""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from rapidfuzz import fuzz, process

from . import classify, issueform
from .lake import Lake
from .models import Shot

log = logging.getLogger(__name__)

INDEX_TTL_S = 3600

TARGET_RATIO = (1.8, 2.3)
TARGET_TIME = (25.0, 32.0)

# Loose input keys people actually type -> Shot fields.
ALIASES = {
    "in": "dose_g",
    "dose": "dose_g",
    "out": "yield_g",
    "yield": "yield_g",
    "time": "time_s",
    "seconds": "time_s",
    "grind": "grinder_setting",
    "grinder": "grinder_setting",
    "temp": "water_temp_c",
    "water_temp": "water_temp_c",
    "method": "brew_method",
    "milk": "is_milk_drink",
    "coffee": "coffee_freetext",
    "bean": "coffee_freetext",
}


# --- catalog matching --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Catalog:
    """One read of the dim_coffee export: its columns and every coffee in it, wholesale listings included."""

    columns: list[str] = field(default_factory=list)
    # (coffee_id, roaster_name, title, is_retail, rank): the rank orders lots that tie on the name, 0 sold now,
    # 1 sold out, 2 a past offering (an export without the columns: all 0)
    rows: list[tuple[str, str, str, bool, int]] = field(default_factory=list)
    index: dict[str, str] = field(default_factory=dict)  # coffee_id -> 'Roaster Name Coffee Title'
    wholesale: frozenset[str] = frozenset()
    ok: bool = False  # the read succeeded


_catalog_cache: dict[str, tuple[float, _Catalog]] = {}  # lake url -> (expires at, catalog)
FAILED_READ_TTL_S = 60  # a failed read is retried after a minute, not trusted for the hour


def retail_only(cols: list[str]) -> str:
    """SQL predicate that leaves out wholesale listings (a roaster's wholesale twin of a retail coffee), for what
    people browse and pick from. A dim_coffee exported before is_wholesale existed (or a row from such a file read
    alongside newer ones) has no flag, so it's kept."""
    return "not coalesce(is_wholesale, false)" if "is_wholesale" in cols else "true"


def _load(lake: Lake | None = None) -> _Catalog:
    """The weekly dim_coffee export, read the way every query of it reads the files (union_by_name, so a column
    only some weeks' files have is there, null for the rest). Its columns, rows, index and wholesale set are read
    together and refreshed together, hourly, so a long-running MCP server picks up new catalog weeks and never
    pairs one week's columns with another's rows."""
    lake = lake or Lake()
    cached = _catalog_cache.get(lake.url)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    cat = _Catalog()
    try:
        src = ", ".join(f"'{s}'" for s in lake.mart_sources("dim_coffee"))
        con = lake.connect()
        scan = f"read_parquet([{src}], union_by_name = true)"
        cols = [r[0] for r in con.execute(f"describe select * from {scan}").fetchall()]
        stock = "in_stock" if "in_stock" in cols else "true"
        status = "catalog_status" if "catalog_status" in cols else "null"
        rows = [
            (
                cid,
                roaster or "",
                title or "",
                bool(retail),
                2 if state == "archived" else 0 if in_stock is not False else 1,
            )
            for cid, roaster, title, retail, in_stock, state in con.execute(
                f"select coffee_id, roaster_name, title, {retail_only(cols)}, {stock}, {status} from {scan}"
            ).fetchall()
        ]
        cat = _Catalog(
            cols,
            rows,
            {cid: f"{roaster} {title}" for cid, roaster, title, _, _ in rows},
            frozenset(cid for cid, _, _, retail, _ in rows if not retail),
            ok=True,
        )
    except Exception as e:  # noqa: BLE001 - no catalog yet just means no matches
        log.info("no catalog rows: %s", e)
    _catalog_cache[lake.url] = (time.monotonic() + (INDEX_TTL_S if cat.ok else FAILED_READ_TTL_S), cat)
    return cat


def mart_columns(lake: Lake | None = None) -> list[str]:
    """The exported dim_coffee's columns: a remote read, so asked with the catalog's hourly refresh (they change
    only with the contract), not on every search. Raises if the read failed: a search without them would quietly
    show wholesale listings as retail."""
    cat = _load(lake)
    if not cat.ok:
        raise RuntimeError("the dim_coffee export couldn't be read; try again in a minute")
    return cat.columns


def _index(lake: Lake | None = None) -> dict[str, str]:
    """coffee_id -> 'Roaster Name Coffee Title', from the weekly dim_coffee export."""
    return _load(lake).index


def _catalog(lake: Lake | None = None) -> list[tuple[str, str, str, bool, int]]:
    """(coffee_id, roaster_name, title, is_retail, rank) for every catalog coffee, wholesale listings included."""
    return _load(lake).rows


def _tokens(s: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1 and t not in {"coffee", "roasters", "co"}
    ]


CONFIDENT_SCORE = 80  # a match made without asking: 80+ and 5 clear of the next
CANDIDATE_SCORE = 50  # offered to pick from: about half the query's words found, or more


def match_score(query: str, label: str) -> int:
    """Mostly: what share of the query's words appear (fuzzily) in the label; WRatio breaks ties."""
    q, lab = _tokens(query), _tokens(label)
    if not q or not lab:
        return 0
    covered = sum(any(fuzz.ratio(a, b) >= 85 for b in lab) for a in q) / len(q)
    return round(70 * covered + 0.3 * fuzz.WRatio(query, label))


def owner_user_id() -> str:
    """The owner's user_id. Shots logged before accounts existed (user_id null) are the owner's."""
    return os.environ.get("COFFEEDATA_OWNER_ID") or "owner"


def fuzzy_match_coffee(text: str, limit: int | None = 3, lake: Lake | None = None) -> list[dict]:
    idx = _index(lake)
    if not text or not idx:
        return []
    # partial_token_set_ratio gives 100 to any title sharing one word with the text, so in a ~40k catalog its 300
    # are an arbitrary few of thousands of ties ("Finca El Puente - Honey Processed" lost to other roasters'
    # "Honey Process"). token_set_ratio's 300 are added: it ties far less on text that names a coffee, but short,
    # generic text ("Ethiopia Guji") still ties across many labels, so even this pool can miss the coffee meant.
    pool = {
        key: label
        for scorer in (fuzz.partial_token_set_ratio, fuzz.token_set_ratio)
        for label, _, key in process.extract(text, idx, scorer=scorer, limit=300)
    }
    scored = sorted(((match_score(text, label), key, label) for key, label in pool.items()), reverse=True)
    return [
        {"coffee_id": key, "label": label.strip(), "score": score} for score, key, label in scored[:limit]
    ]


def search_catalog(text: str, limit: int = 12, lake: Lake | None = None) -> list[dict]:
    """Coffees for a person to pick from: CANDIDATE_SCORE or better, ties sold now first as in match_bag, and no
    wholesale listings unless the text says wholesale (a twin outscores its retail coffee). Raises if the catalog
    couldn't be read, so a failed read isn't shown as no results."""
    mart_columns(lake)
    cat = _load(lake)
    skip = frozenset() if classify.says_wholesale(text) else cat.wholesale
    rank = {cid: r for cid, _, _, _, r in cat.rows}
    found = [
        m
        for m in fuzzy_match_coffee(text, limit=None, lake=lake)
        if m["score"] >= CANDIDATE_SCORE and m["coffee_id"] not in skip
    ]
    return sorted(found, key=lambda m: (-m["score"], rank.get(m["coffee_id"], 0), m["label"]))[:limit]


def _confident(ranked: list[dict], min_score: int) -> str | None:
    """The best candidate's coffee_id when it is 80+ and 5 clear of the next."""
    clear = len(ranked) < 2 or ranked[0]["score"] - ranked[1]["score"] >= 5
    return ranked[0]["coffee_id"] if ranked and ranked[0]["score"] >= min_score and clear else None


def _pick(said: str, ranked: list[dict], lake: Lake | None, min_score: int) -> tuple[str | None, list[dict]]:
    """The confident match among candidates ranked over the whole catalog, and the three to offer.

    `said` is the coffee's own text. When it says wholesale ("Wholesale - 5LB Ethiopia Guji"), every listing
    competes. Otherwise retail coffees are tried first: a wholesale twin's title contains its retail coffee's, so
    it would tie with it and make the match unclear. The retail pick stands only if no wholesale listing outscores
    it (or ties with fewer words the text didn't say); then the whole catalog decides, so a coffee sold only as
    wholesale (or flagged wholesale only by a tag) is still matched from a bag that doesn't say so. Unclear: the
    wholesale listing that beats the retail candidates is offered first, then the retail candidates."""
    if classify.says_wholesale(said):
        return _confident(ranked, min_score), ranked[:3]
    wholesale = _load(lake).wholesale
    asked = set(_tokens(said))

    def key(m: dict) -> tuple[int, int]:
        # On a tie, the label with fewer words the text didn't say is the closer one: a twin "Wholesale - 5LB
        # Ethiopia Guji" adds words to "Ethiopia Guji", while a wholesale "Ethiopia Sidama" is closer to a bag
        # reading "Ethiopia Sidama" than the retail "Ethiopia Sidama Natural" is.
        return m["score"], -len(set(_tokens(m["label"])) - asked)

    retail = [m for m in ranked if m["coffee_id"] not in wholesale]
    top = key(retail[0]) if retail else (-1, 0)
    above = [m for m in ranked if m["coffee_id"] in wholesale and key(m) > top]
    if not above and (best := _confident(retail, min_score)):
        return best, retail[:3]
    # Adding rivals can't make a retail coffee clear, so a match here is a wholesale listing.
    if best := _confident(ranked, min_score):
        return best, ranked[:3]
    return None, (above[:1] + retail + above[1:])[:3]


# --- logging -----------------------------------------------------------------------------------------

_NUMBER = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(g|grams?|s|secs?|seconds?|°?c)?", re.I)


def _number(text: str) -> str:
    """'18.5 g' -> '18.5', '2,5' -> '2.5'. Anything else ('18-19', 'fine') is passed on as-is, so validation
    rejects it with a clear message instead of silently reading a different number."""
    m = _NUMBER.fullmatch(text)
    return m.group(1).replace(",", ".") if m else text


def _yes(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "x", "on", "milk")
    return bool(value)


def normalize(payload: dict) -> dict:
    out: dict = {}
    for k, v in payload.items():
        key = ALIASES.get(k.strip().lower().replace(" ", "_"), k.strip().lower().replace(" ", "_"))
        if isinstance(v, str):
            v = v.strip()
            if v in ("", "_No response_", "None"):
                continue
            if key in ("dose_g", "yield_g", "time_s", "grinder_setting", "water_temp_c"):
                v = _number(v)
            elif key in ("is_milk_drink", "deleted"):
                v = _yes(v)
        out[key] = v
    return out


def match_coffee(
    text: str, lake: Lake | None = None, min_score: int = CONFIDENT_SCORE
) -> tuple[str | None, list[dict]]:
    """The catalog coffee_id for free text, when the best match is confident and clearly ahead."""
    return _pick(text, fuzzy_match_coffee(text, limit=None, lake=lake), lake, min_score)


def match_bag(
    roaster: str | None, coffee: str | None, lake: Lake | None = None, min_score: int = CONFIDENT_SCORE
) -> tuple[str | None, list[dict]]:
    """Match what a bag's label says to the catalog, roaster first, then the coffee among that roaster's.

    Reading the roaster separately is what the photo adds over typed text: a coffee name like "Hambela" is shared
    by dozens of roasters, but only one of them is on the bag. The roaster is matched by name (token set, so
    "Larkspur" finds "Larkspur Roasting Co."); its coffees are then scored on the name alone. With no roaster
    match the whole catalog is searched as for typed text. Same rule as typed text: confident only when the best
    match is 80+ and 5 clear of the next.
    """
    roaster, coffee = (roaster or "").strip(), (coffee or "").strip()
    rows = _catalog(lake)
    if not rows or not (roaster or coffee):
        return None, []
    roasters = {r for _, r, _, _, _ in rows if r}
    # Every roaster that scores 85+, not the top few: names that are token subsets of each other ("Onyx Coffee",
    # "Coffee Lab", "Onyx Coffee Lab") all score 100, and a cut-off could drop the real one.
    found = process.extract(roaster, roasters, scorer=fuzz.token_set_ratio, limit=None) if roaster else []
    near = [name for name, score, _ in found if score >= 85]
    if near and coffee:
        # Ties ("Finca El Puente" against four of one farm's lots) go to what's sold now.
        scored = sorted(
            ((match_score(coffee, t), rank, cid, f"{r} {t}") for cid, r, t, _, rank in rows if r in near),
            key=lambda x: (-x[0], x[1], x[3]),
        )
        matches: list[dict] = [
            {"coffee_id": cid, "label": label, "score": sc} for sc, _, cid, label in scored
        ]
    elif (
        near
    ):  # the roaster is clear but the coffee's name wasn't read: let the person pick, current lots first
        pick = sorted((rank, f"{r} {t}", cid) for cid, r, t, _, rank in rows if r in near)
        matches = [{"coffee_id": cid, "label": label, "score": 0} for _, label, cid in pick]
    else:
        matches = fuzzy_match_coffee(f"{roaster} {coffee}".strip(), limit=None, lake=lake)
    # Only the coffee's text says whether the bag is wholesale: a roaster's name can read as wholesale.
    return _pick(coffee, matches, lake, min_score)


def grinder_id(name: str, lake: Lake | None = None, user_id: str | None = None) -> str:
    """The equipment_id of the user's (default: the owner's) current grinder with this name."""
    user_id = user_id or owner_user_id()
    grinders = [
        e
        for e in (lake or Lake()).latest("equipment", "equipment_id")
        if e["kind"] == "grinder" and (e["user_id"] or owner_user_id()) == user_id
    ]
    # A retired grinder only if no current one has the name; among several, the most recently changed.
    named = [e for e in grinders if (e["name"] or "").strip().lower() == name.strip().lower()]
    for e in sorted(named, key=lambda e: (not e["retired"], e["logged_at"]), reverse=True):
        return e["equipment_id"]
    known = ", ".join(sorted(e["name"] for e in grinders if not e["retired"])) or "none yet"
    raise ValueError(f"no grinder named {name!r} (known: {known})")


def record(shot: Shot, lake: Lake, deleted: bool = False, after: datetime | None = None) -> datetime:
    """Write one shot record. `after` is the shot's previous logged_at: the new record must sort after it
    (logged_at has whole seconds). The file name also carries a random suffix, so two records in the same
    second never collide."""
    now = datetime.now(UTC).replace(microsecond=0)
    if after and now <= after:
        now = after + timedelta(seconds=1)
    lake.write(
        "shots",
        f"shots/{shot.shot_id}__{now:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}.parquet",
        [shot.model_dump() | {"logged_at": now, "deleted": deleted}],
    )
    return now


def last_logged(shot_id: str, lake: Lake) -> datetime | None:
    """When this shot was last recorded, from its own files only (one glob), so a correction sorts after it."""
    con = lake.connect()
    found = []
    for src in lake.sources("shots"):  # staging, then the remote lake: each may have none
        glob = f"{src.rsplit('/', 1)[0]}/{shot_id}__*.parquet"
        try:
            row = con.execute(
                f"select max(logged_at) from read_parquet('{glob}', union_by_name = true)"
            ).fetchone()
        except Exception:  # noqa: BLE001 - duckdb.IOException: no file for this shot there
            continue
        if row and row[0]:
            found.append(row[0])
    return max(found, default=None)


def log_shot(
    payload: dict, lake: Lake | None = None, min_score: int = CONFIDENT_SCORE
) -> tuple[Shot, list[dict]]:
    """Validate and record a shot as its own immutable lake file. Free-text coffee is matched to the catalog.
    To correct a shot, log it again with the same shot_id (latest wins); add deleted=True to retract it."""
    lake = lake or Lake()
    data = normalize(payload)
    deleted = _yes(data.pop("deleted", False))
    data.setdefault("shot_id", str(uuid.uuid4()))
    data.setdefault("brewed_at", datetime.now(UTC).isoformat(timespec="seconds"))
    if name := data.pop("grinder_name", None):
        data["grinder_id"] = grinder_id(name, lake)
    matches: list[dict] = []
    if not data.get("coffee_id") and data.get("coffee_freetext"):
        data["coffee_id"], matches = match_coffee(data["coffee_freetext"], lake, min_score)
    shot = Shot(**data)
    # A correction (an explicit shot_id) must sort after the record it corrects, even within the same second.
    record(
        shot,
        lake,
        deleted,
        after=last_logged(shot.shot_id, lake) if "shot_id" in normalize(payload) else None,
    )
    return shot, matches


def load_shots(lake: Lake | None = None) -> list[Shot]:
    """Current shots: the latest record per shot_id, without retracted ones."""
    lake = lake or Lake()
    cur = lake.connect().execute(f"""
        select * exclude (logged_at, deleted) from {lake.scan("shots")}
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
    return sorted(
        (s for s in shots if s.coffee_freetext and fuzz.WRatio(ref, s.coffee_freetext) >= 85),
        key=lambda s: s.brewed_at,
    )


# --- dial-in advice ----------------------------------------------------------------------------------


def _step(shots: list[Shot]) -> float:
    settings = sorted({s.grinder_setting for s in shots})
    diffs = [b - a for a, b in zip(settings, settings[1:], strict=False) if b > a]
    return min(diffs) if diffs else 1.0


def suggest_next(ref: str, lake: Lake | None = None, finer_is_lower: bool | None = None) -> str:
    """Deterministic espresso dial-in advice from the last shot of this coffee (target ~1:2 in 25-32 s). Which way
    is finer comes from the last shot's grinder in Setup, unless given; lower numbers if neither says."""
    lake = lake or Lake()
    shots = history(ref, lake)
    if not shots:
        return "No shots logged for this coffee yet. Start around 1:2 (e.g. 18 g in, 36 g out) in 25–32 s."
    last = shots[-1]
    if finer_is_lower is None:
        grinder = next(
            (e for e in lake.latest("equipment", "equipment_id") if e["equipment_id"] == last.grinder_id),
            None,
        )
        finer_is_lower = grinder is None or grinder["finer_is_lower"] is not False
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
    elif not TARGET_TIME[0] <= last.time_s <= TARGET_TIME[1]:  # tasted balanced, but outside the time window
        advice = (
            f"It tasted balanced at {last.time_s:g} s, outside the usual 25–32 s. Keep the grind if you "
            f"liked the cup; go {'finer' if last.time_s < TARGET_TIME[0] else 'coarser'} if you want to "
            "bring the time back into the window."
        )
    elif not TARGET_RATIO[0] <= ratio <= TARGET_RATIO[1]:
        target = round(last.dose_g * 2)
        advice = f"Timing is fine; pull to about {target} g out to get closer to 1:2."
    else:
        advice = "That's in the window. Keep the recipe and adjust by taste."
    best = max(shots, key=lambda s: (s.rating, s.brewed_at))
    best_s = (
        (
            f" Your best-rated shot of this coffee ({best.rating}/5): grind {best.grinder_setting:g}, "
            f"{best.dose_g:g} g → {best.yield_g:g} g in {best.time_s:g} s."
        )
        if len(shots) > 1
        else ""
    )
    return f"{desc}. {advice}{best_s}"


# --- GitHub issue form ------------------------------------------------------------------------------

ISSUE_FIELDS = {
    "coffee": "coffee_freetext",
    "grind setting": "grinder_setting",
    "dose (g)": "dose_g",
    "yield (g)": "yield_g",
    "time (s)": "time_s",
    "rating": "rating",
    "taste": "taste",
    "water temp (°c)": "water_temp_c",
    "roast date": "roast_date",
    "brew method": "brew_method",
    "grinder": "grinder_name",
    "notes": "notes",
}


def parse_issue_body(body: str) -> dict:
    """Parse a GitHub issue-form body ('### Label' sections) into shot fields."""
    out: dict = {}
    for label, value in issueform.parse(body).items():
        if label == "milk drink":
            out["is_milk_drink"] = issueform.checked(value)
            continue
        key = ISSUE_FIELDS.get(label)
        if key and value:
            out[key] = value.split(" ")[0] if key == "rating" else value
    return out


def issue_reply(shot: Shot, matches: list[dict], lake: Lake | None = None) -> str:
    lines = [f"Logged shot `{shot.shot_id}`."]
    if shot.coffee_id:
        lines.append(
            f"Matched coffee: **{matches[0]['label'] if matches else shot.coffee_id}** (`{shot.coffee_id}`)."
        )
    elif matches:
        opts = ", ".join(f"`{m['coffee_id']}` ({m['score']})" for m in matches)
        lines.append(f"No confident catalog match for “{shot.coffee_freetext}”. Closest: {opts}.")
    lines.append("")
    lines.append(suggest_next(shot.coffee_id or shot.coffee_freetext or "", lake))
    return "\n".join(lines)
