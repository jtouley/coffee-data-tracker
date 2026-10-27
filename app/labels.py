"""Reading a bag's label with Claude (PLAN, "Where the bag photo is read").

One request per photo: the image, a short instruction, and a JSON schema through structured outputs, so the reply
always parses. Claude only reads the label; matching it to the catalog is deterministic Python
(`coffeedata.shots.match_bag`), and Claude never sees the catalog.

Claude Opus 5.5 is the default until the Opus/Haiku comparison (`app/eval/bag_read.py`) has 20 bags. With Opus the
request opts into the API's server-side refusal fallback, so a declined read is retried on another model instead of
failing. Haiku 5.5 has no server-side fallback, so with Haiku a declined or failed read falls back to typing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"
DEFAULT_MODEL = OPUS
FALLBACK_BETA = "server-side-fallback-2026-07-01"

PROMPT = """This is a photo of a bag of roasted coffee. Read what the label says.

- roaster: the roasting company, as printed (not the café or shop selling it, if that's different).
- coffee: the coffee's own name, as printed: the farm, lot, region or blend name. Not the roaster.
- roast_date: the roast date as YYYY-MM-DD, or null. Labels write it many ways ("R 10.18", "Roasted on 18/10",
  "10/18/26"); use the year of the best-before date or the current year ({year}) when the label has none, and
  prefer month/day for US roasters. A best-before or packed date is not a roast date.
- origin, process: if printed.
- weight_g: the bag's net weight in grams, if printed.
- legible: false if the label can't be read at all (too dark, blurred, not a coffee bag).

Use null for anything that isn't on the label. Don't guess a name that isn't printed."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "legible": {"type": "boolean"},
        "roaster": {"type": ["string", "null"]},
        "coffee": {"type": ["string", "null"]},
        "roast_date": {"type": ["string", "null"]},
        "origin": {"type": ["string", "null"]},
        "process": {"type": ["string", "null"]},
        "weight_g": {"type": ["integer", "null"]},
    },
    "required": ["legible", "roaster", "coffee", "roast_date", "origin", "process", "weight_g"],
    "additionalProperties": False,
}

# Per million tokens, input / output, for the comparison's cost column.
PRICES = {OPUS: (4.00, 20.00), HAIKU: (0.10, 0.50)}


@dataclass
class Read:
    """What one read returned. `label` is None when nothing usable came back; `status` says why."""

    status: str  # read, illegible, refused, failed, no_key
    label: dict | None = None
    raw: str | None = None
    model: str | None = None
    latency_s: float = 0.0
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)

    def roast_date(self) -> date | None:
        try:
            return date.fromisoformat((self.label or {}).get("roast_date") or "")
        except ValueError:
            return None


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model() -> str:
    return os.environ.get("COFFEEDATA_LABEL_MODEL") or DEFAULT_MODEL


def read(jpeg: bytes, model_id: str | None = None, client: Any = None) -> Read:
    """Read one label. Never raises for an API problem: the caller falls back to typing."""
    model_id = model_id or model()
    if client is None:
        if not available():
            return Read("no_key")
        import anthropic

        client = anthropic.Anthropic(max_retries=1, timeout=45.0)
    request: dict[str, Any] = {
        "model": model_id,
        "max_tokens": 2048,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(jpeg).decode(),
                        },
                    },
                    {"type": "text", "text": PROMPT.format(year=date.today().year)},
                ],
            }
        ],
    }
    started = time.monotonic()
    try:
        if model_id == HAIKU:
            msg = client.messages.create(**request)
        else:
            # Opus: a refusal is retried server-side on the model Anthropic recommends for that category.
            msg = client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as e:  # noqa: BLE001 - anthropic.APIError and network errors alike: type it instead
        log.warning("label read failed (%s): %s", model_id, type(e).__name__)
        return Read("failed", model=model_id, latency_s=time.monotonic() - started)
    latency = time.monotonic() - started
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    price_in, price_out = PRICES.get(model_id, (0.0, 0.0))
    cost = (usage["input_tokens"] * price_in + usage["output_tokens"] * price_out) / 1e6
    base = {"model": getattr(msg, "model", model_id), "latency_s": latency, "cost_usd": cost, "usage": usage}
    if msg.stop_reason == "refusal":
        log.info("label read refused (%s)", getattr(getattr(msg, "stop_details", None), "category", None))
        return Read("refused", **base)
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        label = json.loads(text)
    except json.JSONDecodeError:
        return Read("failed", raw=text, **base)
    if not label.get("legible") or not (label.get("roaster") or label.get("coffee")):
        return Read("illegible", label=label, raw=text, **base)
    return Read("read", label=label, raw=text, **base)
