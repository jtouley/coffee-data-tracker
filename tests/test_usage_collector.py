"""The usage collector (app/usage.py) and its event schema (app/usage_events.py): every request gets a 204 and
nothing waits; the IP is masked before anything is kept; nothing written or logged holds a full IP, a cookie or the
user agent; the per-prefix limits, the bot flag, the source parser and the flushes. All values are invented (the
addresses are from the documentation ranges, RFC 5737 and RFC 3849)."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args, get_origin

import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

from app import logs, usage
from app.usage_events import (
    QUERY_MAX,
    Event,
    checked_prefix,
    is_bot,
    mask_ip,
    parse_source,
    referrer_host,
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


class FakeLake(Lake):
    """A local usage lake whose publish can be slow or fail, like Hugging Face on a bad day."""

    def __init__(self, root: Path, fail: int = 0, delay: float = 0.0):
        super().__init__(str(root), tables=USAGE_TABLES)
        self.fail, self.delay, self.publishes = fail, delay, 0

    def publish(self, message: str, attempts: int = 5, **_) -> int:
        time.sleep(self.delay)
        self.publishes += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError(f"hub unreachable from {IP}")  # a message the collector must never log
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
        click(release="20261110T030000-abc123", device="phone", query="denver", cities=["Denver"], states=["CO"],
              origin="Ethiopia", process="natural", state="CO", in_stock=True, sort="asc", results_n=4,
              zero_results=False, coffee_id="example-roaster.com:42", entry=True, utm_source="newsletter")
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
        {"cities": ["x"] * 11},
        {"origin": "o" * 61},
        {"release": "latest"},
        {"event_id": "not-a-uuid"},
        {"roaster_id": "a.com"},  # only a click_out names a roaster
        {"referrer": "https://news.example.com/"},  # only an entry event carries a source
        {"utm_source": "News Letter", "entry": True},
    ],
)
def test_the_wire_schema_is_strict(bad):
    with pytest.raises(ValueError):
        Event.model_validate(search() | bad)


def test_click_out_needs_a_roaster_and_page_views_carry_no_search():
    with pytest.raises(ValueError):
        Event.model_validate({"event": "click_out", "surface": "app_coffee", "coffee_id": "x.com:1"})
    with pytest.raises(ValueError):
        Event.model_validate({"event": "page_view", "surface": "landing", "query": "kenya"})
    assert (
        Event.model_validate({"event": "click_out", "surface": "app_coffee", "roaster_id": "x.com"}).rank
        is None
    )


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
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/131.0 Safari/537.36",
            False,
            True,
        ),
        ("python-requests/2.32", False, True),
        ("curl/8.5.0", False, True),
    ],
)
def test_bot_flag(agent, webdriver, bot):
    assert is_bot(agent, webdriver) is bot


# --- receiving: always 204, never blocking -------------------------------------------------------------------------


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


def test_a_declared_oversized_body_is_never_read(collector):
    async def receive():  # pragma: no cover - must not be called
        raise AssertionError("read a body declared too large")

    sent = []

    async def send(message):
        sent.append(message)

    import asyncio

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/e",
        "client": ("10.0.0.1", 1),
        "headers": [(b"origin", SITE.encode()), (b"content-length", b"999999")],
    }
    asyncio.run(usage.create_app(collector)(scope, receive, send))
    assert sent[0]["status"] == 204 and collector.counts["too_large"] == 1


def test_the_reply_never_waits_for_a_slow_or_failing_publish(tmp_path, clock):
    lake = FakeLake(tmp_path / "usage", fail=1, delay=1.5)
    c = usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)
    with TestClient(usage.create_app(c)) as client:
        for i in range(usage.FLUSH_AT // 20):  # one /24 each: under the per-prefix limit
            r = client.post("/e", headers=headers(ip=f"10.{i}.0.1"), content=body(*[search()] * 20))
            assert r.status_code == 204
        started = time.perf_counter()
        for i in range(5):  # a flush is publishing (slowly, then failing) meanwhile
            r = client.post("/e", headers=headers(ip=f"203.0.{i}.1"), content=body(search()))
            assert r.status_code == 204
        assert time.perf_counter() - started < 1.0
    # the failed publish left its file staged; the shutdown flush published it with the rest
    assert lake.publishes == 2 and not c.unpublished
    assert len(written(lake)) == usage.FLUSH_AT + 5


def test_rate_limits_count_per_masked_prefix(collector, clock, monkeypatch):
    """60 events a minute per /24 (two addresses in it share the count), another /24 unaffected; 2,000 a day."""
    monkeypatch.setattr(collector, "flush_if_due", lambda: None)  # keep every accepted row in memory to count
    for _ in range(3):
        collector.receive(headers(ip="203.0.113.5"), None, body(*[search()] * 20))
    collector.receive(headers(ip="203.0.113.6"), None, body(search()))
    collector.receive(headers(ip="192.0.2.1"), None, body(search()))
    assert collector.counts["accepted"] == 61 and collector.counts["rate_limited"] == 1
    assert {r["ip_prefix"] for r in collector.buffer} == {"203.0.113.0/24", "192.0.2.0/24"}
    for minute in range(1, 40):  # 60 a minute for 39 minutes is 2,340: the day's cap stops it at 2,000
        clock.t = NOW + 60 * minute
        for _ in range(3):
            collector.receive(headers(ip="192.0.2.1"), None, body(*[search()] * 20))
    per_prefix = sum(1 for r in collector.buffer if r["ip_prefix"] == "192.0.2.0/24")
    assert per_prefix == usage.PER_DAY


def test_ipv6_is_masked_to_40_bits(collector):
    collector.receive(headers(ip="2001:db8:abcd:12::99"), None, body(search()))
    assert collector.buffer[0]["ip_prefix"] == "2001:db8:ab00::/40"


def test_the_leftmost_forwarded_entry_is_never_trusted(collector):
    collector.receive(headers(ip="192.0.2.44"), None, body(search()))
    assert collector.buffer[0]["ip_prefix"] == "192.0.2.0/24"  # not SPOOF's /24


def test_flush_writes_one_file_per_day_and_retries_a_failed_publish(tmp_path, clock, monkeypatch):
    lake = FakeLake(tmp_path / "usage", fail=1)
    c = usage.Collector(lambda: lake, origins=frozenset({SITE}), hops=1, clock=clock)
    monkeypatch.setattr(c, "flush_if_due", lambda: None)  # flushed by hand below
    c.receive(headers(), None, body(search()))
    clock.t = datetime(2026, 11, 14, 0, 5, tzinfo=UTC).timestamp()
    c.receive(headers(ip="192.0.2.1"), None, body(click()))
    assert c.flush() == 2 and c.unpublished  # written and staged; the publish failed
    days = sorted(p.parent.name for p in (lake.root / "events").rglob("batch=2*.parquet"))
    assert days == ["day=2026-11-13", "day=2026-11-14"]
    assert (
        c.flush() == 0 and not c.unpublished and lake.publishes == 2
    )  # nothing new, but the staged files went
    assert [r["received_at"].hour for r in written(lake)] == [15, 0]


def test_flush_is_due_at_200_events_or_15_minutes(collector, clock):
    collector.receive(headers(), None, body(search()))
    assert not collector.due()
    clock.t += usage.FLUSH_EVERY
    assert collector.due()
    collector.flush()
    collector.receive(headers(), None, body(*[search()] * 20))
    assert not collector.due()
    collector.counts.clear()
    collector.receive(headers(origin="https://evil.example"), None, b"")
    clock.t += usage.FLUSH_EVERY
    assert collector.due()  # only counts to log: still logged every 15 minutes


def test_memory_is_capped_while_publishing_fails(collector, monkeypatch):
    monkeypatch.setattr(usage, "BUFFER_MAX", 25)
    monkeypatch.setattr(usage, "PER_MINUTE", 1000)
    monkeypatch.setattr(collector, "flush_if_due", lambda: None)
    collector.receive(headers(), None, body(*[search()] * 20))
    collector.receive(headers(), None, body(*[search()] * 20))
    assert len(collector.buffer) == 25 and collector.counts["overflow"] == 15


def test_receive_never_raises(collector, monkeypatch, caplog):
    def boom(*_):
        raise RuntimeError(f"from {IP}")

    monkeypatch.setattr(usage, "mask_ip", boom)
    with caplog.at_level(logging.DEBUG):
        collector.receive(headers(), None, body(search()))
    assert collector.counts["error"] == 1 and IP not in caplog.text


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
        search(query=f"roasters near {EMAIL}", cities=["Denver"], states=["CO"]),
        click(coffee_id="example-roaster.com:7", query=f"roasters near {EMAIL}"),
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
    assert flush.fields["accepted"] == 3 and flush.fields["foreign_origin"] == 1
    # every address-shaped string in the logs is not one: no IPv4 at all
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", log_text)
    for token in re.findall(r"[0-9a-f:]{6,}", log_text):
        with pytest.raises(ValueError):
            ipaddress.ip_address(token)


# --- deployment ----------------------------------------------------------------------------------------------------


def test_the_image_runs_the_collector_when_the_service_says_so():
    cmd = next(line for line in (ROOT / "app/Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    assert "${COFFEEDATA_APP:-app.main}:app" in cmd and "--no-proxy-headers" in cmd
    tf = (ROOT / "src/terraform/usage.tf").read_text()
    assert re.search(r'name\s*=\s*"COFFEEDATA_APP"\s*\n\s*value\s*=\s*"app.usage"', tf)


def test_the_terraform_keeps_to_the_collectors_own_resources():
    """Its own account, one secret it alone reads, scale to zero and at most two instances, the pinned image (Google's
    sample image until one is pinned) owned by Terraform, the request-log exclusion made before the service; the
    service and what hangs off it only with usage_enabled; no grant to the deploy, plan or apply accounts or any
    impersonation role (the plan guard's to refuse, the owner's to grant)."""
    tf = (ROOT / "src/terraform/usage.tf").read_text()
    code = "\n".join(line.split("#")[0] for line in tf.splitlines())
    assert re.search(r"min_instance_count\s*=\s*0", code) and re.search(r"max_instance_count\s*=\s*2", code)
    assert (
        'image = var.usage_image != "" ? var.usage_image : "us-docker.pkg.dev/cloudrun/container/hello"'
        in code
    )
    assert re.search(r"ignore_changes = \[client, client_version, scaling\]", code)  # not the image
    gated = re.findall(r'^resource "(\w+)" "\w+" \{\n  count = local.usage_count', code, re.M)
    assert sorted(gated) == sorted(
        [
            "google_logging_project_exclusion",
            "google_cloud_run_v2_service",
            "google_cloud_run_v2_service_iam_member",
            "google_cloud_run_domain_mapping",
        ]
    )
    assert "google_logging_project_exclusion.usage_requests]" in code  # the service depends on it
    assert re.search(
        r'secret_id = google_secret_manager_secret.usage_token.id\s*\n\s*role\s*=\s*"roles/secretmanager.secretAccessor"',
        code,
    )
    assert "registry-coffee-deploy" not in code and "tf-plan" not in code and "tf-apply" not in code
    assert "google_service_account.deploy" not in code
    for role in (
        "serviceAccountUser",
        "serviceAccountTokenCreator",
        "roles/owner",
        "roles/editor",
        "iam.workloadIdentity",
    ):
        assert role not in code
    assert (
        re.search(r"google_project_iam|google_service_account_iam|google_service_account_key", code) is None
    )
    assert "run.googleapis.com%2Frequests" in code and 'service_name=\\"${local.usage_service}\\"' in code


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


def test_the_logs_never_hold_more_than_counts(collector, caplog):
    with caplog.at_level(logging.DEBUG):
        collector.receive(headers(), None, body(search(query="kenya")))
        collector.flush()
    lines = [r for r in caplog.records if r.name.startswith("app.usage")]
    assert len(lines) == 1
    assert set(lines[0].fields) <= {"written", "files", "published", "accepted", "invalid", "rate_limited",
                                    "opted_out", "foreign_origin", "too_large", "bad_body", "no_address", "overflow",
                                    "error", "write_failed", "publish_failed", "lost"}  # fmt: skip
    assert "kenya" not in caplog.text


def test_the_timer_flushes_without_a_request(collector, clock):
    collector.receive(headers(), None, body(search()))
    clock.t += usage.FLUSH_EVERY
    stop = collector.start_timer(every=0.01)
    try:
        deadline = time.monotonic() + 5
        while collector.buffer and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
    assert collector.buffer == []
    assert all(
        t.name != "usage-flush" or not t.is_alive() or t.join(5) is None for t in threading.enumerate()
    )


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
    _create("google_cloud_run_v2_service.usage[0]", "google_cloud_run_v2_service",
            {"name": "registry-coffee-usage", "location": "us-central1", "ingress": "INGRESS_TRAFFIC_ALL",
             "template": [{"scaling": [{"min_instance_count": 0, "max_instance_count": 2}],
                           "containers": [{"image": "us-docker.pkg.dev/cloudrun/container/hello"}]}]},
            {"template": [{"service_account": True}], "uri": True}),
    _create("google_cloud_run_v2_service_iam_member.usage_public[0]", "google_cloud_run_v2_service_iam_member",
            {"name": "registry-coffee-usage", "location": "us-central1", "role": "roles/run.invoker",
             "member": "allUsers"}),
    _create("google_cloud_run_domain_mapping.usage[0]", "google_cloud_run_domain_mapping",
            {"name": "e.registry.coffee", "location": "us-central1",
             "spec": [{"route_name": "registry-coffee-usage"}]}),
    _create("google_logging_project_exclusion.usage_requests[0]", "google_logging_project_exclusion",
            {"name": "registry-coffee-usage-requests",
             "filter": 'resource.type="cloud_run_revision" AND resource.labels.service_name="registry-coffee-usage" '
                       'AND logName="projects/coffee-data-tracker/logs/run.googleapis.com%2Frequests"'}),
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
