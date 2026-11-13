"""What the usage collector (app/usage.py) accepts and what it writes: the wire schema of one event, the IP mask, the
traffic-source parser, the bot flag and the raw row (lake.SCHEMAS["usage_events"], contracts/internal/usage.odcs.yaml).

Everything here removes data, it never derives any: the query is capped, the IP is cut to its network, a referrer
is cut to its host name, the user agent is reduced to one flag. Location, bot exclusion and every rollup are derived
in dbt after raw (the analytics design, sections 0 and 4).

The wire, from the page scripts on registry.coffee and app.registry.coffee: `POST /e` with a JSON body
`{"events": [<event>, ...]}` (any content type: a no-cors fetch or sendBeacon sends text/plain), at most 4 KB and 20
events. An event has the raw row's names, less those only the collector sets (received_at, source,
referrer_domain, ip_prefix, bot), plus three that are never stored as sent:
- `entry`: true on a page load's first event, the one that carries the traffic source; it is not stored.
- `referrer`: on that event, document.referrer's host name (a full URL is cut to its host here as well).
- `webdriver`: navigator.webdriver; it only sets the bot flag.
Any other name, a wrong type or a value out of range drops the event (extra="forbid").
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from coffeedata.lake import USAGE_VALUES

EVENTS = USAGE_VALUES["event"]
SURFACES = USAGE_VALUES["surface"]
DEVICES = USAGE_VALUES["device"]
SORTS = USAGE_VALUES["sort"]
SOURCES = USAGE_VALUES["source"]
QUERY_MAX = 100
# Our own hosts: a referrer from one of them is `internal`, never a referrer domain.
OWN_DOMAIN = "registry.coffee"
# IPv4 /24 (last octet zeroed); IPv6 /40 (owner's choice over /48, which is often one customer site).
MASK_V4, MASK_V6 = 24, 40

# No control characters in any stored text (a newline or NUL has no business in a search box or a filter value).
_PRINTABLE = r"^[^\x00-\x1f\x7f]*$"
Text = Annotated[str, StringConstraints(max_length=60, pattern=_PRINTABLE)]
# Roaster ids are normalized domains; coffee ids are built from them and the store's product id.
Id = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/|-]+$")]
Utm = Annotated[str, StringConstraints(min_length=1, max_length=40, pattern=r"^[a-z0-9._-]+$")]
# The catalog build the page was showing: lake.new_batch_id()'s shape.
Release = Annotated[str, StringConstraints(pattern=r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")]
HOST = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

# Known crawlers, link previewers, monitors and automation; a page that reports navigator.webdriver is one too.
# Matched case-insensitively against the User-Agent, which is never kept.
BOT_AGENT = re.compile(
    r"bot|crawl|spider|slurp|scrape|fetch|preview|monitor|uptime|lighthouse|pagespeed|headless|phantom|selenium|"
    r"playwright|puppeteer|python|curl|wget|httpie|go-http|java/|okhttp|axios|node-fetch|libwww|scrapy|"
    r"facebookexternalhit|embedly|whatsapp|telegram|discord|slack|bingpreview",
    re.IGNORECASE,
)


class Event(BaseModel):
    """One event as a page script sends it. Strict: an unknown name or a bad value drops the event."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event: Literal["page_view", "search", "filter_state", "click_out"]
    surface: Literal["landing", "catalog", "privacy", "terms", "bot", "app_search", "app_coffee"]
    event_id: uuid.UUID | None = None
    release: Release | None = None
    device: Literal["phone", "desktop"] | None = None
    # As typed; cut to QUERY_MAX characters below rather than refused, so a long search still counts.
    query: Annotated[str, StringConstraints(max_length=1000, pattern=_PRINTABLE)] | None = None
    cities: list[Text] = Field(default_factory=list, max_length=10)
    states: list[Text] = Field(default_factory=list, max_length=10)
    origin: Text | None = None
    process: Text | None = None
    state: Text | None = None
    in_stock: bool | None = None
    sort: Literal["roaster", "asc", "desc"] | None = None
    results_n: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    zero_results: bool | None = None
    roaster_id: Id | None = None
    coffee_id: Id | None = None
    rank: Annotated[int, Field(ge=1, le=100_000)] | None = None
    entry: bool = False
    referrer: Annotated[str, StringConstraints(max_length=300, pattern=_PRINTABLE)] | None = None
    utm_source: Utm | None = None
    utm_medium: Utm | None = None
    utm_campaign: Utm | None = None
    webdriver: bool = False

    @field_validator("event_id", mode="before")
    @classmethod
    def _uuid_from_text(cls, v: Any) -> Any:
        # JSON has no UUID type: strict mode would refuse the string, so parse it here (a bad one still fails).
        return uuid.UUID(v) if isinstance(v, str) and len(v) <= 36 else v

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
        if self.event == "page_view" and (self.query is not None or self.cities or self.states):
            raise ValueError("a page_view carries no search")
        source = (self.referrer, self.utm_source, self.utm_medium, self.utm_campaign)
        if not self.entry and any(v is not None for v in source):
            raise ValueError("only a page load's first event carries its source")
        return self


def mask_ip(ip: str) -> str | None:
    """The network an address belongs to, as text: IPv4 /24, IPv6 /40 (an IPv4-mapped IPv6 address as IPv4). None
    for anything that isn't an address. The full address is never returned, stored or logged."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
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
    return webdriver or not user_agent or bool(BOT_AGENT.search(user_agent))


def hour(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def row(e: Event, ip_prefix: str, bot: bool, now: datetime) -> dict[str, Any]:
    """The raw row (lake.SCHEMAS["usage_events"]) for one accepted event."""
    return {
        "event_id": str(e.event_id or uuid.uuid4()),
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
