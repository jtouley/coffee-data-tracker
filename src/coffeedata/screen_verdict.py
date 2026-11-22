"""Claude's decision on the places the roaster screen's rules leave undecided (docs/design/roaster-screen.md, 4.3).

Who is asked: each place whose latest screen the served build's `screen_scores` puts in review for want of evidence
(the design's rule 5), never a reseller doubt (rule 3: another roaster's name, many vendors, "proudly serving", a
wholesale store), which always goes to a person. What Claude sees: the screen's stored evidence only (the place's
name, domain and tags; its home page's title and text; up to 40 products with title, vendor, type, tags and sizes),
the same fields the screen stored, and nothing fetched again. What it answers: `roaster_online`, `not_roaster` or
`unsure`, and one sentence of reason (screen_definition.md). Unsure is allowed and stays with the owner.

Decisions are raw rows (screen_verdict_batches, screen_verdicts: append-only, private), keyed by the screen they judge
and by the model and the prompt's version (DEFINITION_VERSION: a hash of the prompt, the definition and the answer's
schema, so any change to them is a new version and every place is pending again). dbt joins them into the ops table
screen_scores; nothing here is public. Whether a decision changes a tier is dbt's var screen_claude_promotes, off
until the design's sealed evaluation: until then a decision is recorded and shown, never a Suggested.

The path is the coffee verdicts' (verdict.py): Message Batches with the same Anthropic key (extract.client), work
derived rather than tracked (pending = no decision for this model and version, none in an open batch), and a collect
that names each batch it couldn't read. Cost is bounded by a weekly cap of places sent (WEEKLY_CAP).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import Any, cast

from .lake import SCREEN_VERDICT_LABELS, Lake, new_batch_id

log = logging.getLogger(__name__)

DEFINITION = resources.files("coffeedata").joinpath("screen_definition.md").read_text()
LABELS = SCREEN_VERDICT_LABELS
REASON_CHARS = 300
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "reason"],
    "properties": {"label": {"type": "string", "enum": list(LABELS)}, "reason": {"type": "string"}},
}
PROMPT_VERSION = "screen-verdict-1"
SYSTEM = (
    "You decide whether a place in a list of US cafés and coffee shops is a coffee roaster that sells coffee it "
    "roasts, by the bag, online. Apply this definition exactly, judge only from the evidence given, and answer with "
    "one label and one short sentence of reason.\n\n" + DEFINITION
)


def definition_version(prompt: str = PROMPT_VERSION, system: str = SYSTEM, schema: dict = SCHEMA) -> str:
    """The prompt's version: a hash of its name, the system prompt (the definition in it) and the answer's schema."""
    return hashlib.sha256(
        "\n".join([prompt, system, json.dumps(schema, sort_keys=True)]).encode()
    ).hexdigest()[:12]


# Decisions are kept per version: a change to the prompt, the definition or the schema makes every place pending.
DEFINITION_VERSION = definition_version()

# The model: the design leaves it to validation ("the cheapest model that meets the floors"); until that is measured,
# the cheapest current model, with structured output. COFFEEDATA_SCREEN_VERDICT_MODEL overrides it; the Anthropic key
# is the coffee verdicts' (extract.client), so the job needs no new secret.
MODEL_ENV = "COFFEEDATA_SCREEN_VERDICT_MODEL"
DEFAULT_MODEL = "claude-haiku-5-5"
# At most this many places are sent in one ISO week, whatever is pending (the rest wait for next week).
CAP_ENV = "COFFEEDATA_SCREEN_VERDICT_WEEKLY_CAP"
WEEKLY_CAP = 1_000
HOME_CHARS = 2_000
MAX_PRODUCTS = 40
BATCH_SIZE = 10_000
# Reason codes of the rules' reseller doubts (screen_rules.tier, rule 3): a doubt always goes to a person.
DOUBT_CODES = ("other_roaster ", "vendors ", "serves:", "wholesale ")


def model() -> str:
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL


def weekly_cap() -> int:
    raw = os.environ.get(CAP_ENV, "").strip()
    return int(raw) if raw else WEEKLY_CAP


def undecided(tier: str | None, reasons: list[str] | None) -> bool:
    """A place the rules leave in review for want of evidence (rule 5), not for a reseller doubt (rule 3)."""
    return tier == "review" and not any(r.startswith(DOUBT_CODES) for r in reasons or [])


# --- the evidence ---------------------------------------------------------------------------------------------------


def _sizes(variants: list[dict] | None) -> str:
    out = []
    for v in variants or []:
        title, grams = (v.get("title") or "").strip(), v.get("grams")
        out.append(f"{title} ({grams} g)" if grams else title)
    return "; ".join(s for s in out if s) or "not stated"


def evidence(screen: dict, products: list[dict], place: dict | None) -> str:
    """What Claude sees for one screen: only what the screen stored, in a fixed order, so the same screen always
    gives the same text (and evidence_hash)."""
    place = place or {}
    lines = [
        f"Domain: {screen['roaster_id']}",
        f"Name: {place.get('name') or screen['roaster_id']}",
        f"Listing tags: {', '.join(place.get('tags') or []) or 'none'}",
        f"Home page title: {screen.get('home_title') or 'none'}",
        f"Home page text (first {HOME_CHARS} characters):",
        (screen.get("home_text") or "(no home page text was stored)")[:HOME_CHARS],
        "",
        f"Store products (the first {MAX_PRODUCTS} of {len(products)} on the first catalog page):"
        if products
        else "Store products: none were read (the store's catalog couldn't be read, or it has none).",
    ]
    for p in sorted(products, key=lambda p: str(p.get("product_id")))[:MAX_PRODUCTS]:
        lines.append(
            f"- {p.get('title') or ''} | vendor: {p.get('vendor') or 'none'} | type: {p.get('product_type') or 'none'}"
            f" | tags: {', '.join(p.get('tags') or []) or 'none'} | sizes: {_sizes(p.get('variants'))}"
        )
    return "\n".join(lines)


def evidence_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# --- what is pending ------------------------------------------------------------------------------------------------


def served_scores(lake: Lake) -> list[dict]:
    """The served build's screen_scores (its ops table, beside the release), or [] when none is served or it has
    none. The rules decide in dbt; this step reads their answer, so the two can't disagree."""
    import duckdb

    from . import quality

    rel = quality.served(lake)
    f = quality.ops_files(lake, rel["build_id"]).get("screen_scores") if rel else None
    if not f:
        return []
    with duckdb.connect() as con:
        cur = con.execute(
            f"select * from read_parquet('{f.replace(chr(39), chr(39) * 2)}', hive_partitioning = false)"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _rows(lake: Lake, sql: str, params: dict | None = None) -> list[dict]:
    cur = lake.connect().execute(sql, params or {})
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _decided_or_open(lake: Lake, model_id: str) -> set[str]:
    """Screens with a decision for this model and version, or in a batch not yet collected."""
    vb, sv = lake.scan("screen_verdict_batches"), lake.scan("screen_verdicts")
    params = {"model": model_id, "definition": DEFINITION_VERSION}
    done = _rows(
        lake, f"select distinct screen_id from {sv} where model = $model and definition = $definition", params
    )
    sent = _rows(lake, f"""with b as (select * from {vb} where model = $model and definition = $definition)
        select distinct custom_id as screen_id from b where state = 'submitted'
          and batch_id not in (select batch_id from b where state = 'collected')""", params)  # fmt: skip
    return {r["screen_id"] for r in done + sent}


def pending(lake: Lake, model_id: str, scores: list[dict] | None = None) -> list[dict]:
    """The undecided places' latest screens with no decision for this model and version and none in flight, each
    with its evidence, in screen_id order (stable across runs)."""
    scores = served_scores(lake) if scores is None else scores
    want = {s["screen_id"]: s for s in scores if undecided(s.get("tier"), list(s.get("reasons") or []))}
    want = {k: v for k, v in want.items() if k not in _decided_or_open(lake, model_id)}
    if not want:
        return []
    ids = sorted(want)
    screens = {r["screen_id"]: r for r in _rows(lake,
        f"select * from {lake.scan('screens')} where list_contains($ids, screen_id)", {"ids": ids})}  # fmt: skip
    products: dict[str, list[dict]] = {}
    for p in _rows(lake, f"select * from {lake.scan('screen_products')} where list_contains($ids, screen_id)",
                   {"ids": ids}):  # fmt: skip
        products.setdefault(p["screen_id"], []).append(p)
    places = {r["roaster_id"]: r for r in _rows(lake, f"""select roaster_id, arg_max(name, (added_at, name)) as name,
        arg_max(tags, (added_at, tags)) as tags from {lake.scan('candidates')} where roaster_id is not null
        group by roaster_id""")}  # fmt: skip
    out = []
    for sid in ids:
        s = screens.get(sid)
        if s is None:  # the ops table names a screen the lake no longer holds: nothing to judge from
            continue
        text = evidence(s, products.get(sid, []), places.get(s["roaster_id"]))
        out.append({"screen_id": sid, "roaster_id": s["roaster_id"], "evidence": text,
                    "evidence_hash": evidence_hash(text)})  # fmt: skip
    return out


def sent_this_week(lake: Lake, day: date) -> int:
    """Places sent to Claude in `day`'s ISO week (Monday to Sunday, by when they were sent, UTC), any model or
    version: the cap's count."""
    start = day - timedelta(days=day.weekday())
    end = start + timedelta(days=7)
    vb = lake.scan("screen_verdict_batches")
    row = (
        lake.connect()
        .execute(
            f"select count(distinct custom_id) from {vb} where state = 'submitted' and cast(\"at\" as date) >= $s "
            'and cast("at" as date) < $e',
            {"s": start, "e": end},
        )
        .fetchone()
    )
    return int(row[0]) if row else 0


# --- the request and the answer ---------------------------------------------------------------------------------------


def request(text: str, model_id: str, ttl: str = "1h") -> dict:
    """As verdict.request: the prompt is a fixed, cached system prefix; only the evidence is new input. Low effort and
    a structured answer (label and reason), so a reply is short and always parses."""
    return {
        "model": model_id,
        "max_tokens": 1024,
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral", "ttl": ttl}}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": text}],
    }


def parse(msg: object) -> tuple[str, str] | None:
    """(label, reason) from a reply, or None for a refusal, a cut-off reply or an answer outside the schema: the
    place stays pending and is asked again."""
    if getattr(msg, "stop_reason", None) in ("refusal", "max_tokens"):
        return None
    text = next((b.text for b in getattr(msg, "content", []) if getattr(b, "type", None) == "text"), "")
    try:
        got = json.loads(text)
    except ValueError:
        return None
    if not isinstance(got, dict) or got.get("label") not in LABELS:
        return None
    return got["label"], str(got.get("reason") or "")[:REASON_CHARS]


def _client(client: Any) -> Any:
    if client is not None:
        return client
    from . import extract

    return extract.client()


def submit(lake: Lake, client: Any = None, cap: int | None = None, scores: list[dict] | None = None,
           now: datetime | None = None) -> dict:  # fmt: skip
    """Send pending places to Claude in Message Batches, up to what is left of this week's cap (by when they are
    sent: `now`, UTC). Returns what was pending, sent and left for next week, the batch ids, and an `error` when the
    API refused a batch."""
    model_id = model()
    cap = weekly_cap() if cap is None else cap
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    todo = pending(lake, model_id, scores)
    room = max(0, cap - sent_this_week(lake, now.date()))
    out: dict = {"model": model_id, "definition": DEFINITION_VERSION, "pending": len(todo), "cap": cap,
                 "room": room, "submitted": 0, "batch_ids": []}  # fmt: skip
    todo = todo[:room]
    out["over_cap"] = out["pending"] - len(todo)
    if not todo:
        return out
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    api = _client(client)
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start : start + BATCH_SIZE]
        reqs = [Request(custom_id=r["screen_id"],
                        params=cast(MessageCreateParamsNonStreaming, request(r["evidence"], model_id)))
                for r in chunk]  # fmt: skip
        try:
            batch = api.messages.batches.create(requests=reqs)
        except anthropic.APIError as e:
            log.warning("screen verdict batch submit failed: %s", type(e).__name__)
            code = getattr(e, "status_code", None)
            out["error"] = type(e).__name__ + (f" (HTTP {code})" if code else "")
            break
        lake.write("screen_verdict_batches", f"screen_verdict_batches/state=submitted/{batch.id}.parquet", [
            {"batch_id": batch.id, "state": "submitted", "at": now, "custom_id": r["screen_id"], "model": model_id,
             "definition": DEFINITION_VERSION, "evidence_hash": r["evidence_hash"]}
            for r in chunk])  # fmt: skip
        out["submitted"] += len(reqs)
        out["batch_ids"].append(batch.id)
    return out


def collect(lake: Lake, run_date: str, client: Any = None) -> dict:
    """Store the decisions of finished batches, then mark each batch collected. A failed item stays pending. A batch
    the API couldn't be asked about stays open and is named in `errors` (its id, the error's class and HTTP status,
    never its message), so the command can fail on it after the rest is stored (#209's shape)."""
    vb = lake.scan("screen_verdict_batches")
    open_batches = _rows(lake, f"""select batch_id, any_value(model) as model, any_value(definition) as definition
        from {vb} where state = 'submitted'
          and batch_id not in (select batch_id from {vb} where state = 'collected')
        group by batch_id order by batch_id""")  # fmt: skip
    if not open_batches:
        return {"collected": 0}
    import anthropic

    api = _client(client)
    sent = {(r["batch_id"], r["custom_id"]): r for r in _rows(lake,
        f"select batch_id, custom_id, evidence_hash from {vb} where state = 'submitted'")}  # fmt: skip
    screens = {r["screen_id"]: r["roaster_id"] for r in _rows(lake,
        f"select screen_id, roaster_id from {lake.scan('screens')}")}  # fmt: skip
    stored, not_ended, errors, unparsed = 0, 0, [], 0
    usage: dict[str, int] = {}
    for b in open_batches:
        try:
            if api.messages.batches.retrieve(b["batch_id"]).processing_status != "ended":
                not_ended += 1
                continue
            results = list(api.messages.batches.results(b["batch_id"]))
        except anthropic.APIError as e:
            log.warning("screen verdict batch %s: %s", b["batch_id"], type(e).__name__)
            errors.append({"batch_id": b["batch_id"], "error": type(e).__name__,
                           "http_status": getattr(e, "status_code", None)})  # fmt: skip
            continue
        now = datetime.now(UTC).replace(microsecond=0)
        rows = []
        for res in results:
            if res.result.type != "succeeded":
                continue
            u = getattr(res.result.message, "usage", None)
            for f in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ):
                usage[f] = usage.get(f, 0) + (getattr(u, f, 0) or 0)
            got = parse(res.result.message)
            if got is None:
                unparsed += 1
                continue
            sid = res.custom_id
            rows.append({"decided_at": now, "screen_id": sid, "roaster_id": screens.get(sid),
                         "evidence_hash": (sent.get((b["batch_id"], sid)) or {}).get("evidence_hash"),
                         "model": b["model"], "definition": b["definition"], "label": got[0],
                         "reason": got[1]})  # fmt: skip
        lake.write_partition("screen_verdicts", run_date, new_batch_id(), rows)
        lake.write("screen_verdict_batches", f"screen_verdict_batches/state=collected/{b['batch_id']}.parquet", [
            {"batch_id": b["batch_id"], "state": "collected", "at": now, "custom_id": None, "model": b["model"],
             "definition": b["definition"], "evidence_hash": None}])  # fmt: skip
        stored += len(rows)
    return ({"collected": stored} | ({"unparsed": unparsed} if unparsed else {})
            | ({"usage": usage} if any(usage.values()) else {}) | ({"not_ended": not_ended} if not_ended else {})
            | ({"errors": errors} if errors else {}))  # fmt: skip
