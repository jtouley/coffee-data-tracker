"""What the usage collector (app/usage.py) accepts and what it writes: the wire schema of one event, the IP mask, the
traffic-source parser, the bot flag and the raw row (lake.SCHEMAS["usage_events"], contracts/internal/usage.odcs.yaml).

Everything here removes data, it never derives any: the query is capped, the IP is cut to its network, a referrer
is cut to its host name, the user agent is reduced to one flag. Location, bot exclusion and every rollup are derived
in dbt after raw (the analytics design, sections 0 and 4).

The wire, from the page scripts on registry.coffee and app.registry.coffee, is contracts/internal/usage-wire.schema.json
(written from `wire_schema()` below; `python -m app.usage_events` rewrites it): `POST /e` with a JSON body
`{"events": [<event>, ...]}` (any content type: a no-cors fetch sends text/plain), at most MAX_BODY bytes and
MAX_EVENTS events, from an allowed Origin. An event has the raw row's names, less those only the collector sets
(received_at, source, referrer_domain, ip_prefix, bot), plus three that are never stored as sent:
- `entry`: true on a page load's first event, the one that carries the traffic source; it is not stored.
- `referrer`: on that event, document.referrer's host name (a full URL is cut to its host here as well).
- `webdriver`: navigator.webdriver; it only sets the bot flag.
Any other name, a wrong type or a value out of range drops the event (extra="forbid").

No field but `query` can carry free text that identifies someone: filter values and parsed places refuse `@` and runs
of 7 or more digits; states are two-letter codes; ids follow the catalog's shape; `event_id` is a random version-4
UUID or absent (then null: no id is made up, so a retried event without one can't be told from a new one).
"""

from __future__ import annotations

import ipaddress
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from coffeedata.lake import USAGE_VALUES

EVENTS = USAGE_VALUES["event"]
SURFACES = USAGE_VALUES["surface"]
DEVICES = USAGE_VALUES["device"]
SORTS = USAGE_VALUES["sort"]
SOURCES = USAGE_VALUES["source"]
QUERY_MAX = 100
MAX_BODY = 4096  # bytes, per request
MAX_EVENTS = 20  # per request
# Our own hosts: a referrer from one of them is `internal`, never a referrer domain.
OWN_DOMAIN = "registry.coffee"
# IPv4 /24 (last octet zeroed); IPv6 /40 (owner's choice over /48, which is often one customer site).
MASK_V4, MASK_V6 = 24, 40
# Documentation ranges (RFC 5737, RFC 3849): never routed, so never a real caller; kept usable for tests and canaries.
DOCUMENTATION = tuple(
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
WIRE_SCHEMA = Path(__file__).resolve().parents[1] / "contracts" / "internal" / "usage-wire.schema.json"

# No control characters in any stored text (a newline or NUL has no business in a search box or a filter value).
_PRINTABLE = r"^[^\x00-\x1f\x7f]*$"
# 7 or more digits, with any of the separators a phone or card number is written with between them.
_DIGIT_RUN = re.compile(r"(?:\d[\s().+-]*){7,}")


def _no_identifier(v: str) -> str:
    if "@" in v or _DIGIT_RUN.search(v):
        raise ValueError("looks like an email, phone or card number")
    return v


# A filter value or a place the page's parser read: short, printable, and nothing shaped like an identifier.
Text = Annotated[
    str, StringConstraints(min_length=1, max_length=60, pattern=_PRINTABLE), AfterValidator(_no_identifier)
]
StateCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
# The catalog's ids: a roaster is its normalized domain; a coffee is `<roaster_id>:<the store's product id>`. A
# product id is the platform's id (digits, hex) or, from the generic adapter, a page's JSON-LD productID or sku as
# written: free text (in the 2026-11-09 release, 20 of 45,857 have spaces; the longest is 36 characters). So the tail
# takes any printable text up to 120 characters except `@`: no real id has one, and without it the tail can't hold
# an email or a URL with credentials (user:pass@host). The page copies the id from the catalog, never from what the
# visitor typed, and dbt checks it against the release.
ROASTER_ID = r"[a-z0-9-]+(?:\.[a-z0-9-]+)+"
PRODUCT_ID = r"[^\x00-\x1f\x7f@]{1,120}"
RoasterId = Annotated[str, StringConstraints(min_length=3, max_length=100, pattern=rf"^{ROASTER_ID}$")]
CoffeeId = Annotated[str, StringConstraints(max_length=221, pattern=rf"^{ROASTER_ID}:{PRODUCT_ID}$")]
Utm = Annotated[str, StringConstraints(min_length=1, max_length=40, pattern=r"^[a-z0-9._-]+$")]
# The catalog build the page was showing: lake.new_batch_id()'s shape.
Release = Annotated[str, StringConstraints(pattern=r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")]
EventId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]
HOST = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

# Known crawlers, link previewers, monitors and automation; a page that reports navigator.webdriver is one too.
# Matched case-insensitively against the User-Agent, which is never kept. Anchored so real browsers don't match: a
# word ending in "bot" (Googlebot, TelegramBot, Discordbot, Slackbot) but not CUBOT phones; WhatsApp's link preview
# (its UA starts with WhatsApp/) but not a browser inside WhatsApp or Telegram.
BOT_AGENT = re.compile(
    r"(?<!cu)bot\b|crawl|spider|slurp|headlesschrome|phantomjs|selenium|playwright|puppeteer|lighthouse|"
    r"pagespeed|^python|python-requests|curl/|wget/|httpie|go-http-client|^java/|okhttp|axios/|node-fetch|"
    r"libwww|scrapy|facebookexternalhit|embedly|bingpreview|^whatsapp/",
    re.IGNORECASE,
)


class Event(BaseModel):
    """One event as a page script sends it. Strict: an unknown name or a bad value drops the event."""

    model_config = ConfigDict(extra="forbid", strict=True, title="UsageEvent")

    event: Literal[EVENTS]  # type: ignore[valid-type]
    surface: Literal[SURFACES]  # type: ignore[valid-type]
    event_id: EventId | None = None
    release: Release | None = None
    device: Literal[DEVICES] | None = None  # type: ignore[valid-type]
    # As typed, not scrubbed (the owner's decision; a dbt detector counts any that look personal); cut to QUERY_MAX
    # characters below rather than refused, so a long search still counts.
    query: Annotated[str, StringConstraints(max_length=1000, pattern=_PRINTABLE)] | None = None
    cities: list[Text] = Field(default_factory=list, max_length=10)
    states: list[StateCode] = Field(default_factory=list, max_length=10)
    origin: Text | None = None
    process: Text | None = None
    state: StateCode | None = None
    in_stock: bool | None = None
    sort: Literal[SORTS] | None = None  # type: ignore[valid-type]
    results_n: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    zero_results: bool | None = None
    roaster_id: RoasterId | None = None
    coffee_id: CoffeeId | None = None
    rank: Annotated[int, Field(ge=1, le=100_000)] | None = None
    entry: bool = False
    referrer: Annotated[str, StringConstraints(max_length=300, pattern=_PRINTABLE)] | None = None
    utm_source: Utm | None = None
    utm_medium: Utm | None = None
    utm_campaign: Utm | None = None
    webdriver: bool = False

    @field_validator("query")
    @classmethod
    def _cap_query(cls, v: str | None) -> str | None:
        return v[:QUERY_MAX] if v is not None else None

    @model_validator(mode="after")
    def _fits_its_event(self) -> Event:
        click = (self.roaster_id, self.coffee_id, self.rank)
        if self.event == "click_out" and self.roaster_id is None:
            raise ValueError("a click_out names its roaster")
        if self.event != "click_out" and any(v is not None for v in click):
            raise ValueError("only a click_out carries a roaster, coffee or rank")
        if self.coffee_id is not None and not self.coffee_id.startswith(f"{self.roaster_id}:"):
            raise ValueError("a coffee belongs to the roaster clicked")
        if self.event == "page_view" and (self.query is not None or self.cities or self.states):
            raise ValueError("a page_view carries no search")
        source = (self.referrer, self.utm_source, self.utm_medium, self.utm_campaign)
        if not self.entry and any(v is not None for v in source):
            raise ValueError("only a page load's first event carries its source")
        return self


def mask_ip(ip: str) -> str | None:
    """The network an address belongs to, as text: IPv4 /24, IPv6 /40 (an IPv4-mapped IPv6 address as IPv4). None
    for anything that isn't a public address (private, link-local, loopback, multicast, reserved: no place to look
    up), except the documentation ranges. The full address is never returned, stored or logged."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (not addr.is_global or addr.is_multicast) and not any(addr in net for net in DOCUMENTATION):
        return None
    bits = MASK_V4 if addr.version == 4 else MASK_V6
    return str(ipaddress.ip_network(f"{addr}/{bits}", strict=False))


def referrer_host(referrer: str) -> str | None:
    """A referrer reduced to its host name, lower-case, no port; None if it has none or it is an IP address (an
    address in a referrer could be someone's). Never the path or the query."""
    text = referrer.strip()
    try:
        host = urlsplit(text if "://" in text else f"//{text}").hostname
    except ValueError:
        return None
    host = (host or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return host if HOST.fullmatch(host) else None


def own_host(host: str) -> bool:
    return host == OWN_DOMAIN or host.endswith("." + OWN_DOMAIN)


def parse_source(e: Event) -> dict[str, str | None]:
    """The traffic source of a page load's first event: its utm_* tags if it has any, else its referrer's host (our
    own hosts read `internal`), else `direct`. Events after the first carry no source: all null."""
    out: dict[str, str | None] = {
        "source": None,
        "referrer_domain": None,
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": None,
    }
    if not e.entry:
        return out
    if e.utm_source or e.utm_medium or e.utm_campaign:
        return out | {
            "source": "utm",
            "utm_source": e.utm_source,
            "utm_medium": e.utm_medium,
            "utm_campaign": e.utm_campaign,
        }
    host = referrer_host(e.referrer) if e.referrer else None
    if host and own_host(host):
        return out | {"source": "internal"}
    if host:
        return out | {"source": "referrer", "referrer_domain": host}
    # A referrer with no usable host (an IP address, a malformed value) is still a referral, from somewhere unnamed.
    return out | {"source": "referrer" if e.referrer else "direct"}


def is_bot(user_agent: str | None, webdriver: bool) -> bool:
    """A known bot's user agent, none at all, or a page driven by automation (navigator.webdriver)."""
    agent = user_agent.strip() if user_agent is not None else ""
    return webdriver or not agent or bool(BOT_AGENT.search(agent))


def hour(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def row(e: Event, ip_prefix: str, bot: bool, now: datetime) -> dict[str, Any]:
    """The raw row (lake.SCHEMAS["usage_events"]) for one accepted event."""
    return {
        "event_id": e.event_id,
        "received_at": hour(now),
        "event": e.event,
        "surface": e.surface,
        "release": e.release,
        "device": e.device,
        "query": e.query,
        "cities": list(e.cities),
        "states": list(e.states),
        "origin": e.origin,
        "process": e.process,
        "state": e.state,
        "in_stock": e.in_stock,
        "sort": e.sort,
        "results_n": e.results_n,
        "zero_results": e.zero_results,
        "roaster_id": e.roaster_id,
        "coffee_id": e.coffee_id,
        "rank": e.rank,
        **parse_source(e),
        "ip_prefix": checked_prefix(ip_prefix),
        "bot": bot,
    }


def checked_prefix(prefix: str) -> str:
    """The one value a row's ip_prefix may hold: a network no narrower than the mask. A full address, or a network
    narrower than /24 or /40, raises, so nothing can write one."""
    net = ipaddress.ip_network(prefix, strict=True)
    if net.prefixlen > (MASK_V4 if net.version == 4 else MASK_V6):
        raise ValueError("ip_prefix is narrower than the mask")
    return str(net)


def wire_schema() -> dict[str, Any]:
    """The wire format as JSON Schema (2020-12): the envelope, each event's fields, and in `description` and the
    `x-collector` keys what JSON Schema can't say (the cross-field rules, the size cap, the headers)."""
    event = Event.model_json_schema(mode="validation")
    defs = event.pop("$defs", {})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/jtouley/coffee-data-tracker/blob/main/contracts/internal/usage-wire.schema.json",
        "title": "Usage collector wire format",
        "description": (
            "The body of POST /e to the usage collector (app/usage.py). Generated by app/usage_events.py; "
            "tests/test_usage_collector.py fails when it and the code disagree. Per event, beyond the fields' own "
            "rules: a click_out names its roaster_id, and only a click_out carries roaster_id, coffee_id or rank; a "
            "coffee_id starts with its roaster_id and ':'; a page_view carries no query, cities or states; only an "
            "entry event (a page load's first) carries referrer or utm_*; query is cut to 100 characters, never "
            "refused for length up to 1000; origin, process and cities refuse '@' and runs of 7 or more digits (with "
            "phone or card separators). An event that breaks a rule is dropped alone; the reply is 204 whatever "
            "happens."
        ),
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_EVENTS,
                "items": {"$ref": "#/$defs/UsageEvent"},
            }
        },
        "required": ["events"],
        "additionalProperties": False,
        "$defs": {**defs, "UsageEvent": event},
        "x-collector": {
            "method": "POST",
            "path": "/e",
            "maxBodyBytes": MAX_BODY,
            "contentType": "any (a no-cors fetch sends text/plain); the body is parsed as JSON",
            "credentials": "never: fetch with credentials 'omit', mode 'no-cors', keepalive",
            "origin": "required, one of COFFEEDATA_USAGE_ORIGINS (registry.coffee, www, app); a missing or 'null' "
            "Origin is dropped, so the fetch sets referrerPolicy 'strict-origin-when-cross-origin'",
            "dropped": "Sec-GPC: 1 or DNT: 1 (the page should send nothing then)",
            "reply": "204, empty, always",
        },
    }


def render_wire_schema() -> str:
    return json.dumps(wire_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit(
            "usage: python -m app.usage_events --write   (rewrites contracts/internal/usage-wire.schema.json)"
        )
    WIRE_SCHEMA.write_text(render_wire_schema())
    print(WIRE_SCHEMA)
