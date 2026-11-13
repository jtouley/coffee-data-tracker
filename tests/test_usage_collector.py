"""The usage collector (app/usage.py) and its event schema (app/usage_events.py): every request gets a 204; the IP is
masked before anything is kept; nothing written or logged holds a full IP, a cookie or the user agent; the limits,
the bot flag, the source parser, the flushes and their bounds; the wire schema; the Terraform and the bootstrap
grants it needs. All values are invented (the addresses are from the documentation ranges, RFC 5737 and RFC 3849)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import threading
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args, get_origin

import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

from app import logs, usage
from app.limits import FixedWindows, forwarded_client
from app.usage_events import (
    QUERY_MAX,
    Event,
    checked_prefix,
    is_bot,
    mask_ip,
    parse_source,
    referrer_host,
    render_wire_schema,
    row,
)
from coffeedata import raw_contracts
from coffeedata.lake import SCHEMAS, USAGE_TABLES, USAGE_VALUES, Lake

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://registry.coffee"
IP = "203.0.113.77"  # the canary's address
SPOOF = "198.51.100.9"  # what a client could put in X-Forwarded-For itself
COOKIE = "session=c4n4ry-s3ss10n-v4lu3-0123456789abcdef"
EMAIL = "canary@example.com"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
NOW = datetime(2026, 11, 13, 15, 42, 7, tzinfo=UTC).timestamp()
EVENT_ID = "3f2b8c1e-9a4d-4e7f-8b21-6c0d5e4f3a29"  # version 4


class FakeLake(Lake):
    """A local usage lake whose writes and publishes can be slow or fail, like Hugging Face on a bad day."""

    def __init__(self, root: Path, fail: int = 0, delay: float = 0.0, fail_writes: int = 0):
        super().__init__(str(root), tables=USAGE_TABLES)
        self.fail, self.delay, self.fail_writes, self.publishes = fail, delay, fail_writes, 0

    def write(self, table, rel, rows):
        if self.fail_writes:
            self.fail_writes -= 1
            raise OSError(f"disk full near {IP}")  # a message the collector must never log
        return super().write(table, rel, rows)

    def publish(self, message: str, attempts: int = 5, **_) -> int:
        time.sleep(self.delay)
        self.publishes += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError(f"hub unreachable from {IP}")
        return 1


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def lake(tmp_path) -> FakeLake:
    return FakeLake(tmp_path / "usage")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def collector(lake, clock) -> usage.Collector:
    return usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)


def headers(ip: str = IP, **extra: str) -> dict[str, str]:
    return {"origin": SITE, "user-agent": UA, "x-forwarded-for": f"{SPOOF}, {ip}", **extra}


def body(*events: dict) -> bytes:
    return json.dumps({"events": list(events)}).encode()


def search(**kw) -> dict:
    return {"event": "search", "surface": "catalog", "query": "ethiopia natural", "results_n": 12} | kw


def click(**kw) -> dict:
    return {"event": "click_out", "surface": "catalog", "roaster_id": "example-roaster.com", "rank": 3} | kw


def prefix_ip(i: int) -> str:
    """An address in the i-th /40 of the IPv6 documentation range: a separate prefix for each i < 256."""
    return f"2001:db8:{i:02x}00::1"


def written(lake: Lake) -> list[dict]:
    files = sorted(p for p in (lake.root / "events").rglob("*.parquet") if "schema" not in p.name)
    return [r for f in files for r in pq.read_table(f).to_pylist()]


def _choices(annotation) -> list[str]:
    """A Literal's values, through an Optional."""
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    return next(_choices(a) for a in get_args(annotation) if a is not type(None))


# --- the schema --------------------------------------------------------------------------------------------------


def test_rows_follow_the_usage_contract():
    """Exactly the contract's columns, in the lake's order; every coded column's values are the contract's, and the
    wire schema's choices are the same lists."""
    e = Event.model_validate(
        click(release="20261110T030000-abc123", device="phone", event_id=EVENT_ID, origin="Ethiopia",
              process="natural", state="CO", in_stock=True, sort="asc", results_n=4, zero_results=False,
              coffee_id="example-roaster.com:42", entry=True, utm_source="newsletter")
    )  # fmt: skip
    r = row(e, "203.0.113.0/24", False, datetime.fromtimestamp(NOW, UTC))
    assert list(r) == SCHEMAS["usage_events"].names == raw_contracts.columns("usage", "usage_events")
    contract = next(o for o in raw_contracts.load("usage")["schema"] if o["name"] == "usage_events")
    enums = {p["name"]: [v["value"] for v in p["enum"]] for p in contract["properties"] if "enum" in p}
    assert enums == {k: list(v) for k, v in USAGE_VALUES.items()}
    for column, values in enums.items():
        assert r[column] is None or r[column] in values, column
        if column in Event.model_fields:
            assert _choices(Event.model_fields[column].annotation) == values, column
    assert r["source"] == "utm" and r["received_at"] == datetime(2026, 11, 13, 15, tzinfo=UTC)
    assert r["event_id"] == EVENT_ID


def test_an_event_without_an_id_is_stored_without_one():
    """No id is made up: a retry of an event without one can't be told from a new one, and a made-up id would hide
    that."""
    r = row(Event.model_validate(search()), "203.0.113.0/24", False, datetime.fromtimestamp(NOW, UTC))
    assert r["event_id"] is None


def test_the_wire_schema_file_is_the_codes():
    """contracts/internal/usage-wire.schema.json is what `python -m app.usage_events --write` writes, the usage
    contract links it, and `make contracts` rewrites it."""
    assert (ROOT / "contracts/internal/usage-wire.schema.json").read_text() == render_wire_schema()
    doc = json.loads(render_wire_schema())
    assert doc["properties"]["events"]["maxItems"] == usage.MAX_EVENTS
    assert doc["x-collector"]["maxBodyBytes"] == usage.MAX_BODY
    assert set(doc["$defs"]["UsageEvent"]["properties"]) == set(Event.model_fields)
    assert doc["$defs"]["UsageEvent"]["additionalProperties"] is False
    links = [d["url"] for d in raw_contracts.load("usage")["authoritativeDefinitions"]]
    assert raw_contracts.REPO + "contracts/internal/usage-wire.schema.json" in links
    assert "python -m app.usage_events --write" in (ROOT / "Makefile").read_text()


def test_no_row_can_hold_a_full_address():
    """ip_prefix takes only a /24 or /40 network; the wire refuses any field the schema doesn't name (an `ip`), and no
    stored column is meant for an address or a user agent."""
    assert mask_ip(IP) == "203.0.113.0/24"
    assert mask_ip("2001:db8:1234:5678::1") == "2001:db8:1200::/40"
    assert mask_ip("::ffff:203.0.113.77") == "203.0.113.0/24"
    assert mask_ip("not an address") is None and mask_ip("") is None
    for full in (IP, f"{IP}/32", "203.0.113.0/25", "2001:db8::1", "2001:db8:1200::/48"):
        with pytest.raises(ValueError):
            checked_prefix(full)
    assert checked_prefix("203.0.113.0/24") == "203.0.113.0/24" and checked_prefix("2001:db8::/32")
    with pytest.raises(ValueError):
        row(Event.model_validate(search()), IP, False, datetime.fromtimestamp(NOW, UTC))
    for extra in ({"ip": IP}, {"client_ip": IP}, {"user_agent": UA}, {"cookie": COOKIE}, {"session": "x"}):
        with pytest.raises(ValueError):
            Event.model_validate(search() | extra)
    names = set(SCHEMAS["usage_events"].names)
    assert not names & {"ip", "ip_address", "client_ip", "user_agent", "ua", "cookie", "session_id", "user_id",
                        "visitor_id", "page_load_id", "referrer", "url", "path"}  # fmt: skip


@pytest.mark.parametrize(
    "address", ["10.1.2.3", "192.168.0.9", "172.16.5.5", "127.0.0.1", "169.254.1.1", "fe80::1", "::1", "fc00::5",
                "224.0.0.1", "0.0.0.0", "::ffff:10.0.0.1"]
)  # fmt: skip
def test_addresses_that_arent_public_have_no_prefix(address):
    """Private, link-local, loopback, multicast and unspecified addresses name no place: no prefix, and the event is
    dropped as `no_address` (the documentation ranges stay usable for tests)."""
    assert mask_ip(address) is None


def test_query_is_kept_as_typed_and_capped():
    typed = "  Kenya AA, washed — from Nairobi? " + "x" * 200
    e = Event.model_validate(search(query=typed))
    assert e.query == typed[:QUERY_MAX] and len(e.query) == 100
    assert (
        Event.model_validate(search(query=f"email me {EMAIL}")).query == f"email me {EMAIL}"
    )  # never scrubbed
    with pytest.raises(ValueError):
        Event.model_validate(search(query="line\nbreak"))


@pytest.mark.parametrize(
    "bad",
    [
        {"event": "scroll"},
        {"surface": "admin"},
        {"device": "tablet"},
        {"sort": "random"},
        {"results_n": -1},
        {"results_n": "12"},
        {"cities": ["denver"] * 11},
        {"origin": "o" * 61},
        {"origin": EMAIL},  # identifiers outside the query
        {"process": "call 555-123-4567"},
        {"cities": ["denver", "+1 (303) 555 0100"]},
        {"origin": "4111 1111 1111 1111"},
        {"states": ["Colorado"]},
        {"states": ["co"]},
        {"state": "C0"},
        {"release": "latest"},
        {"event_id": "not-a-uuid"},
        {"event_id": str(uuid.uuid1())},  # not random: a version-1 UUID carries a clock and a node
        {"event_id": EVENT_ID.upper()},
        {"event_id": 7},
        {"roaster_id": "example-roaster.com"},  # only a click_out names a roaster
        {"referrer": "https://news.example.com/"},  # only an entry event carries a source
        {"utm_source": "News Letter", "entry": True},
    ],
)
def test_the_wire_schema_is_strict(bad):
    with pytest.raises(ValueError):
        Event.model_validate(search() | bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"roaster_id": "Example-Roaster.com"},
        {"roaster_id": "localhost"},
        {"roaster_id": "a" * 120 + ".com"},
        {"roaster_id": "x.com/path"},
        {"coffee_id": "other-roaster.com:42"},  # a coffee belongs to the roaster clicked
        {"coffee_id": "example-roaster.com:"},
        {"coffee_id": "example-roaster.com:a b"},
        {"coffee_id": "example-roaster.com:" + "9" * 80},
    ],
)
def test_ids_follow_the_catalogs_shape(bad):
    with pytest.raises(ValueError):
        Event.model_validate(click() | bad)


def test_click_out_needs_a_roaster_and_page_views_carry_no_search():
    with pytest.raises(ValueError):
        Event.model_validate({"event": "click_out", "surface": "app_coffee", "coffee_id": "x.com:1"})
    with pytest.raises(ValueError):
        Event.model_validate({"event": "page_view", "surface": "landing", "query": "kenya"})
    ok = Event.model_validate({"event": "click_out", "surface": "app_coffee", "roaster_id": "x.com",
                               "coffee_id": "x.com:8812345678901"})  # fmt: skip
    assert (
        ok.rank is None and ok.coffee_id == "x.com:8812345678901"
    )  # a store's long numeric id is fine there


@pytest.mark.parametrize(
    ("sent", "source", "domain"),
    [
        ({"utm_source": "newsletter", "utm_medium": "email", "referrer": "news.example.com"}, "utm", None),
        ({"referrer": "https://news.example.com:8443/a/b?q=secret#frag"}, "referrer", "news.example.com"),
        ({"referrer": "News.Example.COM."}, "referrer", "news.example.com"),
        ({"referrer": "app.registry.coffee"}, "internal", None),
        ({"referrer": "https://registry.coffee/catalog/?q=x"}, "internal", None),
        ({"referrer": "http://203.0.113.5/page"}, "referrer", None),  # an address is never kept
        ({"referrer": "http://[2001:db8::1]/"}, "referrer", None),
        ({"referrer": ""}, "direct", None),
        ({}, "direct", None),
    ],
)
def test_source_is_the_tags_else_the_referrers_host_else_direct(sent, source, domain):
    out = parse_source(
        Event.model_validate({"event": "page_view", "surface": "landing", "entry": True} | sent)
    )
    assert out["source"] == source and out["referrer_domain"] == domain
    assert all("/" not in (v or "") and "secret" not in (v or "") for v in out.values())


def test_only_a_page_loads_first_event_carries_a_source():
    out = parse_source(Event.model_validate(search()))
    assert set(out.values()) == {None}
    assert referrer_host("https://a.example.com/x?y=z") == "a.example.com" and referrer_host("::::") is None


@pytest.mark.parametrize(
    ("agent", "webdriver", "bot"),
    [
        (UA, False, False),
        (UA, True, True),
        (None, False, True),
        ("", False, True),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", False, True),
        ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", False, True),
        ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/131.0 Safari/537.36", False, True),
        ("python-requests/2.32", False, True),
        ("curl/8.5.0", False, True),
        ("TelegramBot (like TwitterBot)", False, True),  # Telegram's link preview
        ("WhatsApp/2.23.20.0 A", False, True),  # WhatsApp's link preview
        ("Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)", False, True),
        ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)", False, True),
        # real people's browsers that the first patterns flagged (review finding 8)
        ("Mozilla/5.0 (Linux; Android 12; CUBOT KINGKONG 7) AppleWebKit/537.36 Chrome/119.0 Mobile Safari/537.36",
         False, False),
        ("Mozilla/5.0 (Linux; Android 13; CUBOT_X70) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
         False, False),
        ("Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/537.36 Chrome/125.0 Mobile "
         "Safari/537.36 Telegram-Android/11.2.3 (Google Pixel 8; Android 14; SDK 34; HIGH)", False, False),
        ("Mozilla/5.0 (Linux; Android 14; SM-S918B; wv) AppleWebKit/537.36 Chrome/125.0 Mobile Safari/537.36 "
         "WhatsApp/2.24.10.78", False, False),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 "
         "[FBAN/FBIOS;FBAV/470.0]", False, False),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15 "
         "Slack_SSB/4.38.125", False, False),
    ],
)  # fmt: skip
def test_bot_flag(agent, webdriver, bot):
    assert is_bot(agent, webdriver) is bot


# --- receiving: always 204 --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "send", "reason"),
    [
        ("POST", {"headers": headers(**{"sec-gpc": "1"}), "content": body(search())}, "opted_out"),
        ("POST", {"headers": headers(dnt="1"), "content": body(search())}, "opted_out"),
        ("POST", {"headers": headers(origin="https://evil.example"), "content": body(search())}, "foreign_origin"),
        ("POST", {"headers": headers(origin="null"), "content": body(search())}, "foreign_origin"),
        ("POST", {"headers": {k: v for k, v in headers().items() if k != "origin"}, "content": body(search())},
         "foreign_origin"),
        ("POST", {"headers": headers(), "content": b"x" * (usage.MAX_BODY + 1)}, "too_large"),
        ("POST", {"headers": headers(), "content": body(*[search()] * (usage.MAX_EVENTS + 1))}, "bad_body"),
        ("POST", {"headers": headers(), "content": b"{not json"}, "bad_body"),
        ("POST", {"headers": headers(), "content": b'{"events": [], "ip": "203.0.113.77"}'}, "bad_body"),
        ("POST", {"headers": headers(), "content": body({"event": "search", "surface": "catalog", "x": 1})},
         "invalid"),
        ("POST", {"headers": {"origin": SITE, "x-forwarded-for": "garbage"}, "content": body(search())},
         "no_address"),
        ("POST", {"headers": headers(ip="10.0.0.7"), "content": body(search())}, "no_address"),
        ("OPTIONS", {"headers": headers()}, None),
        ("GET", {"headers": headers()}, None),
    ],
)  # fmt: skip
def test_every_request_gets_an_empty_204_and_nothing_bad_is_kept(collector, method, send, reason):
    client = TestClient(usage.create_app(collector))  # no lifespan: the counts stay to be read
    r = client.request(method, "/e", **send)
    assert (r.status_code, r.content) == (204, b"")
    assert "set-cookie" not in r.headers and "access-control-allow-origin" not in r.headers
    assert r.headers["cache-control"] == "no-store"
    assert collector.buffer == [] and not collector.counts.get("accepted")
    if reason:
        assert collector.counts[reason] >= 1, reason


def test_health_and_other_paths(collector):
    with TestClient(usage.create_app(collector)) as client:
        assert client.get("/healthz").text == client.get("/health").text == "ok"
        assert client.post("/anything", headers=headers(), content=body(search())).status_code == 404
    assert collector.counts.get("accepted") is None


def _asgi(app, scope_headers: list[tuple[bytes, bytes]], messages: list[dict]) -> list[dict]:
    sent: list[dict] = []
    queue = list(messages)

    async def receive():
        if not queue:
            raise AssertionError("read past the body")
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/e",
        "client": ("192.0.2.1", 1),
        "headers": scope_headers,
    }
    asyncio.run(app(scope, receive, send))
    return sent


def test_a_declared_oversized_body_is_never_read(collector):
    sent = _asgi(
        usage.create_app(collector), [(b"origin", SITE.encode()), (b"content-length", b"999999")], []
    )
    assert sent[0]["status"] == 204 and collector.counts["too_large"] == 1


def test_a_client_that_goes_away_is_counted_as_aborted(collector):
    sent = _asgi(usage.create_app(collector), [(b"origin", SITE.encode())],
                 [{"type": "http.request", "body": b'{"ev', "more_body": True}, {"type": "http.disconnect"}])  # fmt: skip
    assert sent == [] and collector.counts["aborted"] == 1 and not collector.counts.get("too_large")


def test_rate_limits_count_per_masked_prefix(collector, clock):
    """600 events a minute per /24 (two addresses in it share the count), another /24 unaffected; 10,000 a day."""
    for _ in range(30):
        collector.receive(headers(ip="203.0.113.5"), None, body(*[search()] * 20))
    collector.receive(headers(ip="203.0.113.6"), None, body(search()))
    collector.receive(headers(ip="192.0.2.1"), None, body(search()))
    assert collector.counts["accepted"] == 601 and collector.counts["rate_limited"] == 1
    assert {r["ip_prefix"] for r in collector.buffer} == {"203.0.113.0/24", "192.0.2.0/24"}
    for minute in range(1, 18):  # 600 a minute for 17 minutes is 10,200: the day's limit stops it at 10,000
        clock.t = NOW + 60 * minute
        for _ in range(30):
            collector.receive(headers(ip="192.0.2.1"), None, body(*[search()] * 20))
    per_prefix = sum(1 for r in collector.buffer if r["ip_prefix"] == "192.0.2.0/24")
    assert per_prefix == usage.PER_DAY


def test_the_instance_ceiling_and_daily_cap(collector, clock, monkeypatch):
    monkeypatch.setattr(usage, "CEILING_PER_MINUTE", 30)
    monkeypatch.setattr(usage, "DAILY_CAP", 50)
    for i in range(3):
        collector.receive(headers(ip=prefix_ip(i)), None, body(*[search()] * 20))
    assert collector.counts["accepted"] == 30 and collector.counts["ceiling"] == 30
    clock.t += 60
    for i in range(3, 6):
        collector.receive(headers(ip=prefix_ip(i)), None, body(*[search()] * 20))
    assert collector.counts["accepted"] == 50 and collector.counts["daily_cap"] >= 10


def test_fixed_windows_keep_one_count_per_key_and_window():
    w = FixedWindows()
    limits = {"k": (3, 60.0)}
    assert [w.take(limits, 100.0) for _ in range(4)] == [True, True, True, False]
    assert w.take(limits, 121.0)  # the next minute
    assert len(w.counts) == 2  # one integer per window, not one per hit


def test_fixed_windows_sweep_past_windows_at_most_once_a_minute(monkeypatch):
    import app.limits as limits

    monkeypatch.setattr(limits, "SWEEP_AT", 5)
    w = FixedWindows()
    for i in range(10):
        w.take({f"k{i}": (5, 60.0)}, 100.0)
    w.take(
        {"x": (5, 60.0)}, 100.0
    )  # minute 1 was swept once already (nothing was past): no sweep per request
    assert len(w.counts) == 11
    w.take({"y": (5, 60.0)}, 130.0)  # the next minute sweeps once: minute 1's eleven counts go
    assert set(w.counts) == {("y", 60.0, 2)}


def test_ipv6_is_masked_to_40_bits(collector):
    collector.receive(headers(ip="2001:db8:abcd:12::99"), None, body(search()))
    assert collector.buffer[0]["ip_prefix"] == "2001:db8:ab00::/40"


def test_the_leftmost_forwarded_entry_is_never_trusted(collector):
    collector.receive(headers(ip="192.0.2.44"), None, body(search()))
    assert collector.buffer[0]["ip_prefix"] == "192.0.2.0/24"  # not SPOOF's /24
    assert forwarded_client(f"{SPOOF}, 192.0.2.44", "10.0.0.1", 1) == "192.0.2.44"
    assert forwarded_client("", "10.0.0.1", 1) == "10.0.0.1"


def test_memory_and_staging_are_capped(collector, monkeypatch):
    monkeypatch.setattr(usage, "BUFFER_MAX", 25)
    collector.receive(headers(), None, body(*[search()] * 20))
    collector.receive(headers(), None, body(*[search()] * 20))
    assert len(collector.buffer) == 25 and collector.counts["overflow"] == 15
    monkeypatch.setattr(usage, "BUFFER_MAX", 1000)
    monkeypatch.setattr(usage, "STAGED_MAX", 40)
    collector.staged = 10  # written, a publish failed: 25 in memory + 10 staged leaves room for 5
    collector.receive(headers(ip="192.0.2.1"), None, body(*[search()] * 20))
    assert len(collector.buffer) == 30 and collector.counts["staged_full"] == 15


def test_receive_never_raises(collector, monkeypatch, caplog):
    def boom(*_):
        raise RuntimeError(f"from {IP}")

    monkeypatch.setattr(usage, "mask_ip", boom)
    with caplog.at_level(logging.DEBUG):
        collector.receive(headers(), None, body(search()))
    assert collector.counts["error"] == 1 and IP not in caplog.text


# --- flushing ------------------------------------------------------------------------------------------------------


def test_flush_writes_one_file_per_day_and_retries_a_failed_publish(tmp_path, clock):
    lake = FakeLake(tmp_path / "usage", fail=1)
    c = usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)
    c.receive(headers(), None, body(search()))
    clock.t = datetime(2026, 11, 14, 0, 5, tzinfo=UTC).timestamp()
    c.receive(headers(ip="192.0.2.1"), None, body(click()))
    assert c.flush() == 2 and c.staged == 2  # written; the publish failed, so they wait in staging
    days = sorted(p.parent.name for p in (lake.root / "events").rglob("batch=2*.parquet"))
    assert days == ["day=2026-11-13", "day=2026-11-14"]
    clock.t += usage.PUBLISH_EVERY
    assert c.due()  # staged files make a flush due
    assert c.flush() == 0 and c.staged == 0 and lake.publishes == 2  # nothing new, but the staged files went
    assert [r["received_at"].hour for r in written(lake)] == [15, 0]


def test_a_failed_write_puts_its_rows_back(tmp_path, clock, caplog):
    """The partial write: day one written and staged, day two's write fails and its rows go back in memory; the next
    flush writes them and publishes both. Nothing is lost, and the warning names only the error's type."""
    lake = FakeLake(tmp_path / "usage")
    c = usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)
    c.receive(headers(), None, body(search()))
    clock.t = datetime(2026, 11, 14, 0, 5, tzinfo=UTC).timestamp()
    c.receive(headers(ip="192.0.2.1"), None, body(click(), click()))
    real = lake.write
    calls = []

    def second_fails(table, rel, rows):
        calls.append(rel)
        if len(calls) == 2:
            raise OSError(f"disk full near {IP}")
        return real(table, rel, rows)

    lake.write = second_fails
    with caplog.at_level(logging.DEBUG):
        assert c.flush() == 1
    assert (
        len(c.buffer) == 2 and lake.publishes == 1 and c.staged == 0
    )  # day one published, day two back in memory
    flush_line = next(r for r in caplog.records if r.getMessage().startswith("usage flush"))
    assert flush_line.levelno == logging.WARNING and flush_line.fields["write_failed"] == 1
    assert "lost" not in flush_line.fields and IP not in caplog.text
    clock.t += usage.PUBLISH_EVERY
    assert c.flush() == 2 and len(written(lake)) == 3 and c.buffer == []


def test_flush_is_due_at_200_events_or_15_minutes_but_never_twice_a_minute(collector, clock):
    collector.receive(headers(), None, body(search()))
    assert not collector.due()  # the first minute after start, and only one event
    clock.t += usage.FLUSH_EVERY
    assert collector.due()
    collector.flush()
    for i in range(10):  # 200 events, at once
        collector.receive(headers(ip=prefix_ip(i)), None, body(*[search()] * 20))
    assert not collector.due()  # a flush ran less than a minute ago
    clock.t += usage.PUBLISH_EVERY
    assert collector.due()
    collector.flush()
    collector.receive(headers(origin="https://evil.example"), None, b"")
    clock.t += usage.PUBLISH_EVERY
    assert not collector.due()  # only counts: they wait for 15 minutes
    clock.t += usage.FLUSH_EVERY
    assert collector.due()


def test_the_request_that_finds_a_flush_due_flushes_before_its_reply(collector, lake, clock):
    """The flush runs while that request is open (the CPU is the instance's only then), at most once a minute: ten
    batches of 200 in one minute make one commit."""
    client = TestClient(usage.create_app(collector))
    clock.t += usage.PUBLISH_EVERY
    for i in range(20):
        r = client.post("/e", headers=headers(ip=prefix_ip(i)), content=body(*[search()] * 20))
        assert r.status_code == 204
    assert (
        lake.publishes == 1 and len(written(lake)) == usage.FLUSH_AT
    )  # flushed by the 10th request, then gated
    assert len(collector.buffer) == 200


def test_a_slow_publish_delays_one_reply_no_longer_than_flush_wait(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(usage, "FLUSH_WAIT", 0.2)
    lake = FakeLake(tmp_path / "usage", delay=1.0)
    c = usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)
    client = TestClient(usage.create_app(c))
    clock.t += usage.PUBLISH_EVERY
    for i in range(9):
        client.post("/e", headers=headers(ip=prefix_ip(i)), content=body(*[search()] * 20))
    started = time.perf_counter()
    assert (
        client.post("/e", headers=headers(ip=prefix_ip(9)), content=body(*[search()] * 20)).status_code == 204
    )
    assert time.perf_counter() - started < 0.8
    started = time.perf_counter()
    assert client.post("/e", headers=headers(ip=prefix_ip(10)), content=body(search())).status_code == 204
    assert time.perf_counter() - started < 0.5  # the next request isn't held by the flush still running


def test_shutdown_publishes_within_its_budget_and_counts_what_is_lost(tmp_path, clock, caplog):
    lake = FakeLake(tmp_path / "usage", fail=5)
    c = usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)
    c.receive(headers(), None, body(search(), search(), search()))
    with caplog.at_level(logging.DEBUG):
        c.shutdown(budget=2.0)
    lost = next(r for r in caplog.records if r.getMessage().startswith("usage shutdown"))
    assert lost.levelno == logging.ERROR and lost.fields == {"lost": 3, "finished": True}
    assert lake.publishes == 1  # one attempt: no backoff inside the 10 seconds


def test_shutdown_never_waits_past_its_budget_for_a_stuck_flush(collector, caplog):
    collector.receive(headers(), None, body(search(), search()))
    collector._flushing.acquire()  # a flush stuck in a hung publish
    try:
        started = time.perf_counter()
        with caplog.at_level(logging.DEBUG):
            collector.shutdown(budget=0.5)
        assert time.perf_counter() - started < 1.5
    finally:
        collector._flushing.release()
    lost = next(r for r in caplog.records if r.getMessage().startswith("usage shutdown"))
    assert lost.fields == {"lost": 2, "finished": False}


def test_a_clean_shutdown_logs_no_loss(collector, lake, caplog):
    collector.receive(headers(), None, body(search()))
    with caplog.at_level(logging.DEBUG):
        collector.shutdown(budget=2.0)
    assert len(written(lake)) == 1 and lake.publishes == 1
    assert not any(r.getMessage().startswith("usage shutdown") for r in caplog.records)


def test_flush_lines_log_at_warning_when_dropped_and_error_when_lost(collector, clock, caplog, monkeypatch):
    monkeypatch.setattr(usage, "PER_MINUTE", 1)
    with caplog.at_level(logging.DEBUG):
        collector.receive(headers(), None, body(search(), search()))
        collector.flush()
    line = next(r for r in caplog.records if r.getMessage().startswith("usage flush"))
    assert line.levelno == logging.WARNING and line.fields["rate_limited"] == 1
    caplog.clear()
    counts = usage.Counter({"lost": 4})
    with caplog.at_level(logging.DEBUG):
        collector._log(counts, 0, 0, None)
    assert caplog.records[0].levelno == logging.ERROR


# --- the canary ----------------------------------------------------------------------------------------------------


def test_canary_leaves_no_ip_or_cookie_in_the_lake_or_logs(lake, collector, caplog, capsys):
    """An email-like search, a session cookie, an Authorization header and X-Forwarded-For through the whole
    collector, to the lake and its log: the full IP, the cookie and the user agent are nowhere; the IP's /24 is only
    in ip_prefix; the email is only in raw's query (kept as typed, design section 3), never in a log."""
    sent = headers(cookie=COOKIE, authorization="Bearer c4n4ry-t0k3n", referer=f"{SITE}/catalog/?q={EMAIL}")
    events = [
        {
            "event": "page_view",
            "surface": "catalog",
            "entry": True,
            "referrer": f"https://mail.example.com/?u={IP}",
        },
        search(query=f"roasters near {EMAIL}", cities=["denver"], states=["CO"], event_id=EVENT_ID),
        click(coffee_id="example-roaster.com:7"),
    ]
    formatter = logs.JsonFormatter()
    # the client's exit runs the lifespan's shutdown, which flushes
    with caplog.at_level(logging.DEBUG), TestClient(usage.create_app(collector)) as client:
        assert client.post("/e", headers=sent, content=body(*events)).status_code == 204
        assert (
            client.post("/e", headers=sent | {"origin": "https://x.example"}, content=b"{}").status_code
            == 204
        )
    rows = written(lake)
    assert len(rows) == 3
    assert {r["ip_prefix"] for r in rows} == {"203.0.113.0/24"}
    assert rows[0]["source"] == "referrer" and rows[0]["referrer_domain"] == "mail.example.com"

    raw_text = json.dumps(rows, default=str)
    files = b"".join(p.read_bytes() for p in lake.root.rglob("*") if p.is_file())
    log_text = "\n".join([formatter.format(r) for r in caplog.records] + list(capsys.readouterr()))
    secrets = [IP, SPOOF, COOKIE, COOKIE.split("=")[1], "c4n4ry-t0k3n", UA, "iPhone"]
    for text in (raw_text, log_text):
        for s in secrets:
            assert s not in text
    for s in secrets:
        assert s.encode() not in files
    assert "203.0.113.0" not in raw_text.replace('"ip_prefix": "203.0.113.0/24"', "")
    assert EMAIL in raw_text and EMAIL not in log_text
    assert re.search(r"usage flush: 3 events in 1 files", log_text)
    flush = next(r for r in caplog.records if r.getMessage().startswith("usage flush"))
    assert (
        flush.fields["accepted"] == 3
        and flush.fields["foreign_origin"] == 1
        and flush.fields["published"] == 3
    )
    # every address-shaped string in the logs is not one: no IPv4 at all
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", log_text)
    for token in re.findall(r"[0-9a-f:]{6,}", log_text):
        with pytest.raises(ValueError):
            ipaddress.ip_address(token)


def test_the_logs_never_hold_more_than_counts(collector, caplog):
    with caplog.at_level(logging.DEBUG):
        collector.receive(headers(), None, body(search(query="kenya")))
        collector.flush()
    lines = [r for r in caplog.records if r.name.startswith("app.usage")]
    assert len(lines) == 1
    allowed = {"written", "files", "published_ok", "staged", "published", "accepted", "invalid", "aborted",
               "opted_out", "foreign_origin", "too_large", "bad_body", "no_address", "error", *usage.WARN, *usage.LOSS}  # fmt: skip
    assert set(lines[0].fields) <= allowed
    assert "kenya" not in caplog.text


# --- deployment ----------------------------------------------------------------------------------------------------


def test_the_image_runs_the_collector_when_the_service_says_so():
    cmd = next(line for line in (ROOT / "app/Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    assert "${COFFEEDATA_APP:-app.main}:app" in cmd and "--no-proxy-headers" in cmd
    tf = (ROOT / "src/terraform/usage.tf").read_text()
    assert re.search(r'name\s*=\s*"COFFEEDATA_APP"\s*\n\s*value\s*=\s*"app.usage"', tf)


def _code(path: str) -> str:
    return "\n".join(line.split("#")[0] for line in (ROOT / path).read_text().splitlines())


def test_the_terraform_keeps_to_the_collectors_own_resources():
    """Its own account, one secret it alone reads, scale to zero and at most two instances, the pinned image (Google's
    sample image until one is pinned) owned by Terraform, no custom domain, the request-log exclusion made before the
    service; the service and what hangs off it only with usage_enabled; no grant to the deploy, plan or apply
    accounts or any impersonation role (the plan guard's to refuse, the owner's to grant)."""
    code = _code("src/terraform/usage.tf")
    assert re.search(r"min_instance_count\s*=\s*0", code) and re.search(r"max_instance_count\s*=\s*2", code)
    assert (
        'image = var.usage_image != "" ? var.usage_image : "us-docker.pkg.dev/cloudrun/container/hello"'
        in code
    )
    assert re.search(r"ignore_changes = \[client, client_version, scaling\]", code)  # not the image
    assert re.search(r'timeout\s*=\s*"30s"', code) and re.search(r"cpu_idle\s*=\s*true", code)
    assert usage.FLUSH_WAIT + 5 <= 30
    gated = re.findall(r'^resource "(\w+)" "\w+" \{\n  count = local.usage_count', code, re.M)
    assert sorted(gated) == sorted(
        [
            "google_logging_project_exclusion",
            "google_cloud_run_v2_service",
            "google_cloud_run_v2_service_iam_member",
            "google_monitoring_alert_policy",
        ]
    )
    assert "google_logging_project_exclusion.usage_requests]" in code  # the service depends on it
    assert "domain_mapping" not in code and 'registry.coffee"' not in code
    assert re.search(r'secret_id = google_secret_manager_secret.usage_token.id\s*\n\s*role\s*=\s*"roles/secretmanager.secretAccessor"', code)  # fmt: skip
    assert "registry-coffee-deploy" not in code and "tf-plan" not in code and "tf-apply" not in code
    for role in ("serviceAccountUser", "serviceAccountTokenCreator", "roles/owner", "roles/editor",
                 "iam.workloadIdentity"):  # fmt: skip
        assert role not in code
    assert (
        re.search(r"google_project_iam|google_service_account_iam|google_service_account_key", code) is None
    )
    assert "run.googleapis.com%2Frequests" in code and 'service_name=\\"${local.usage_service}\\"' in code
    assert 'jsonPayload.logger=\\"app.usage\\"' in code and '"severity>=WARNING"' in code  # the loss alert


def test_the_bootstrap_gives_the_apply_account_exactly_what_the_collector_needs():
    """actAs on the collector's account (and no other new one), log exclusions only (no sinks, buckets or delete),
    and the plan account their read; the main root's plan stays green without them while usage_enabled is off."""
    code = _code("src/terraform/bootstrap/iam.tf")
    assert 'usage            = "${var.service}-usage"' in code
    assert 'for_each           = toset(["runtime", "ingest", "ingest_scheduler", "usage"])' in code
    perms = dict(
        re.findall(r'role_id\s*=\s*"(tf\w*LogExclusions)"[^}]*?permissions\s*=\s*\[([^\]]*)\]', code, re.S)
    )
    assert {k: sorted(re.findall(r'"([^"]+)"', v)) for k, v in perms.items()} == {
        "tfApplyLogExclusions": ["logging.exclusions.create", "logging.exclusions.get", "logging.exclusions.list",
                                 "logging.exclusions.update"],
        "tfPlanLogExclusions": ["logging.exclusions.get", "logging.exclusions.list"],
    }  # fmt: skip
    apply = code[code.index('resource "google_project_iam_member" "tf_apply"') :]
    plan = code[code.index('resource "google_project_iam_member" "tf_plan"') :]
    assert "google_project_iam_custom_role.tf_log_exclusions.id" in apply[: apply.index("])")]
    assert "google_project_iam_custom_role.tf_log_exclusions_reader.id" in plan[: plan.index("])")]
    assert "logging.sinks" not in code and "roles/logging" not in code
    variables = _code("src/terraform/variables.tf")
    assert re.search(r'variable "usage_enabled" \{[^}]*default\s*=\s*false', variables, re.S)
    assert not re.search(r"^usage_enabled\s*=\s*true", (ROOT / "src/terraform/ci.tfvars").read_text(), re.M)


def test_the_collector_url_is_in_one_checked_in_place():
    """config/usage.toml holds it: empty until the service exists, then Cloud Run's deterministic address, the same
    shape as Terraform's output usage_url."""
    url = tomllib.loads((ROOT / "config/usage.toml").read_text())["collector_url"]
    shape = r"https://registry-coffee-usage-[0-9]+\.us-central1\.run\.app"
    assert url == "" or re.fullmatch(shape, url)
    out = _code("src/terraform/outputs.tf")
    assert '"https://${local.usage_service}-${data.google_project.this.number}.${var.region}.run.app"' in out


def _usage_image_pattern() -> re.Pattern:
    tf = (ROOT / "src/terraform/variables.tf").read_text()
    block = tf[tf.index('variable "usage_image"') :]
    hcl = re.search(r'can\(regex\("(.+?)", var\.usage_image\)\)', block).group(1)
    return re.compile(hcl.replace("\\\\", "\\"))  # HCL's "\\." is the regex \.


@pytest.mark.parametrize(
    ("image", "ok"),
    [
        ("us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app@sha256:" + "a" * 64, True),
        ("us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app:latest", False),
        ("us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/ingest@sha256:" + "a" * 64, False),
        ("docker.io/library/app@sha256:" + "a" * 64, False),
        ("us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app@sha256:" + "a" * 63, False),
    ],
)
def test_usage_image_is_the_apps_image_by_digest(image, ok):
    assert bool(_usage_image_pattern().fullmatch(image)) is ok


def test_deploy_keeps_the_pinned_usage_image_and_never_deploys_it():
    """deploy.yml tags what ci.tfvars pins so the registry's cleanup keeps it; it never deploys the collector."""
    wf = (ROOT / ".github/workflows/deploy.yml").read_text()
    step = wf[wf.index("- name: Keep the usage collector's pinned image") :]
    step = step[: step.index("\n\n")]
    assert "src/terraform/ci.tfvars" in step and 'tags add "$usage" "$IMAGE:pinned-usage-' in step
    assert "run deploy" not in step and "registry-coffee-usage" not in wf
    keep = (ROOT / "src/terraform/artifact_registry.tf").read_text()
    assert re.search(r'tag_prefixes = \["pinned"', keep)  # pinned-usage-* is kept
    tfvars = (ROOT / "src/terraform/ci.tfvars").read_text()
    pinned = re.findall(r'^usage_image *= *"(.*)"$', tfvars, re.M)
    assert all(_usage_image_pattern().fullmatch(p) for p in pinned)


def _create(address: str, rtype: str, after: dict, unknown: dict | None = None) -> dict:
    return {
        "address": address,
        "type": rtype,
        "name": address.split(".")[1].split("[")[0],
        "mode": "managed",
        "change": {"actions": ["create"], "before": None, "after": after, "after_unknown": unknown or {}},
    }


# What `terraform plan` shows for usage.tf on a project that has the rest already, with usage_enabled on: seven
# creates (the first three alone with it off). Values Terraform learns only at apply (the account's email, the
# secret's id) are unknown, as they are in the real plan.
USAGE_PLAN = [
    _create("google_service_account.usage", "google_service_account",
            {"account_id": "registry-coffee-usage", "display_name": "registry.coffee usage collector (runtime)"},
            {"email": True, "member": True, "name": True}),
    _create("google_secret_manager_secret.usage_token", "google_secret_manager_secret",
            {"secret_id": "USAGE_HF_TOKEN", "replication": [{"auto": [{}]}]}, {"id": True, "name": True}),
    _create("google_secret_manager_secret_iam_member.usage_token", "google_secret_manager_secret_iam_member",
            {"role": "roles/secretmanager.secretAccessor"}, {"member": True, "secret_id": True}),
    _create("google_logging_project_exclusion.usage_requests[0]", "google_logging_project_exclusion",
            {"name": "registry-coffee-usage-requests",
             "filter": 'resource.type="cloud_run_revision" AND resource.labels.service_name="registry-coffee-usage" '
                       'AND logName="projects/coffee-data-tracker/logs/run.googleapis.com%2Frequests"'}),
    _create("google_cloud_run_v2_service.usage[0]", "google_cloud_run_v2_service",
            {"name": "registry-coffee-usage", "location": "us-central1", "ingress": "INGRESS_TRAFFIC_ALL",
             "template": [{"scaling": [{"min_instance_count": 0, "max_instance_count": 2}],
                           "containers": [{"image": "us-docker.pkg.dev/cloudrun/container/hello"}]}]},
            {"template": [{"service_account": True}], "uri": True}),
    _create("google_cloud_run_v2_service_iam_member.usage_public[0]", "google_cloud_run_v2_service_iam_member",
            {"name": "registry-coffee-usage", "location": "us-central1", "role": "roles/run.invoker",
             "member": "allUsers"}),
    _create("google_monitoring_alert_policy.usage_dropped[0]", "google_monitoring_alert_policy",
            {"display_name": "registry.coffee: usage events dropped or lost", "combiner": "OR"},
            {"notification_channels": True, "name": True}),
]  # fmt: skip

PINNED = "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app@sha256:" + "c" * 64


def usage_bump(old: str, new: str) -> dict:
    """The PR that deploys a collector version: the service's image, from one digest (or the placeholder) to another."""
    before = {"name": "registry-coffee-usage", "template": [{"containers": [{"image": old}]}]}
    after = {"name": "registry-coffee-usage", "template": [{"containers": [{"image": new}]}]}
    return {
        "address": "google_cloud_run_v2_service.usage[0]",
        "type": "google_cloud_run_v2_service",
        "name": "usage",
        "mode": "managed",
        "change": {"actions": ["update"], "before": before, "after": after, "after_unknown": {}},
    }


def load_guard(path: Path):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(f"tf_plan_guard_{abs(hash(path))}", path)
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    spec.loader.exec_module(guard)
    return guard


def guard_results(guard, changes: list[dict]) -> dict[str, list[str]]:
    doc = json.loads((ROOT / "tests/fixtures/tf_plan/good.json").read_text())
    doc["resource_changes"] += changes
    return {name: bad for name, _, bad in guard.run_guards(guard.load(json.dumps(doc)))}


@pytest.mark.parametrize(
    "changes",
    [
        USAGE_PLAN[:3],  # usage_enabled off: the account and the secret
        USAGE_PLAN,  # usage_enabled on
        [usage_bump("us-docker.pkg.dev/cloudrun/container/hello", PINNED)],  # the first pinned digest
        [usage_bump(PINNED, PINNED.replace("c" * 64, "d" * 64))],  # a later one
    ],
)
def test_the_plan_guard_passes_the_collectors_resources(changes):
    """scripts/tf_plan_guard.py over each plan this PR's Terraform makes, added to the guard's own good plan: every
    guard passes, so nothing here trips them for the wrong reason (no grant to a guarded account, no impersonation
    role, the service's image Google's placeholder or the app's by digest)."""
    assert set(
        re.findall(r'^resource "(\w+)" "(\w+)"', (ROOT / "src/terraform/usage.tf").read_text(), re.M)
    ) == {(rc["type"], rc["name"]) for rc in USAGE_PLAN}
    results = guard_results(load_guard(ROOT / "scripts" / "tf_plan_guard.py"), changes)
    assert results and all(bad == [] for bad in results.values()), results


def test_flush_threads_never_outlive_the_test():
    assert not [
        t for t in threading.enumerate() if t.name == "usage-shutdown" and t.is_alive() and not t.daemon
    ]


def test_a_request_without_a_user_agent_is_stored_as_a_bot_and_answered(collector):
    """No User-Agent header (None, not ""): the reply is still 204 and the row says bot, never a crash or a null."""
    sent = {k: v for k, v in headers().items() if k != "user-agent"}
    r = TestClient(usage.create_app(collector)).post(
        "/e", headers=sent | {"user-agent": ""}, content=body(search())
    )
    assert r.status_code == 204
    collector.receive(sent, None, body(search()))  # the header absent altogether
    assert [row["bot"] for row in collector.buffer] == [True, True]
    assert not collector.counts.get("error")
