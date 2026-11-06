"""Smoke, ingestion (`make smoke`; CI's smoke job, part ingestion). Every connector parses its recorded storefront
responses, and `coffeedata crawl --only <roaster>` runs against a fake store on this machine (a real HTTP server,
reached through the crawler's own client): a 429 then a 200, and an edge's 202 challenge, land as the fetch, block
and manifest rows the weekly run reads. Nothing leaves the machine."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from coffeedata import cli, connectors, control, http, registry
from coffeedata.adapters import generic
from coffeedata.lake import Lake

from .conftest import FIX, load_roasters, recorded_items
from .smoke import NoNetwork

pytestmark = pytest.mark.smoke

# The recorded responses per connector. square_online has none recorded (test_adapters builds its payload inline).
RECORDED = {
    "shopify": ["counterculturecoffee.com", "onyxcoffeelab.com", "intelligentsia.com"],
    "woocommerce": ["anaheimcoffee.com"],
    "squarespace": ["vividcoffee.com"],
    "generic": ["wix_product_jsonld.html"],
}
NOT_RECORDED = {"square_online"}
SG = {"sg-captcha": "challenge", "server": "nginx"}


def test_the_network_guard_refuses_outside_connections(no_network):
    with socket.socket() as s, pytest.raises(NoNetwork):
        s.connect(("93.184.216.34", 443))
    with pytest.raises(httpx.ConnectError):
        httpx.get("https://example.com/", timeout=2)


@pytest.mark.parametrize("connector", sorted(connectors.CONNECTORS))
def test_every_connector_parses_its_recorded_storefront(connector):
    assert set(RECORDED) | NOT_RECORDED == set(connectors.CONNECTORS)  # a new connector brings a recording
    if connector in NOT_RECORDED:
        pytest.skip(f"no recorded {connector} storefront")
    if connector == "generic":
        r = load_roasters()["1802roasters.com"]
        items = [
            generic.parse_jsonld(p, f"https://{r.host}/product-page/x", r)
            for p in generic.jsonld_products((FIX / "wix_product_jsonld.html").read_text())
        ]
        assert items
    else:
        parsed = recorded_items(load_roasters())
        items = [i for rid in RECORDED[connector] for i in parsed[rid]]
        assert {i.roaster_id for i in items} == set(RECORDED[connector])
    for i in items:
        assert i.title and i.coffee_id.startswith(f"{i.roaster_id}:") and i.variants
    assert any(v.price_usd for i in items for v in i.variants)


class FakeStores(ThreadingHTTPServer):
    """Stores by Host header: robots.txt allows everything; onyxcoffeelab.com's first catalog page answers 429 once,
    then its recorded products, then an empty page; anaheimcoffee.com's catalog answers SiteGround's 202 challenge."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.asked: list[tuple[str, str, int]] = []  # (host, path, status)
        self.onyx = load(FIX / "shopify_onyxcoffeelab.json")
        self.throttled = False


def load(path):
    return json.loads(path.read_text())


class Handler(BaseHTTPRequestHandler):
    server: FakeStores

    def log_message(self, *a):
        pass

    def do_GET(self):
        host, url = self.headers["Host"].split(":")[0], urlsplit(self.path)
        page = int(parse_qs(url.query).get("page", ["1"])[0])
        if url.path == "/robots.txt":
            self.answer(200, "User-agent: *\nAllow: /\n", "text/plain")
        elif host == "onyxcoffeelab.com" and url.path == "/products.json":
            if page == 1 and not self.server.throttled:
                self.server.throttled = True
                self.answer(429, "slow down", "text/plain", {"Retry-After": "1"})
            else:
                self.answer(200, json.dumps({"products": self.server.onyx["products"] if page == 1 else []}))
        elif host == "anaheimcoffee.com" and url.path == "/wp-json/wc/store/v1/products":
            self.answer(202, "<html>checking your browser</html>", "text/html", SG)
        else:
            self.answer(404, "not found", "text/plain")

    def answer(self, status, body, ctype="application/json", headers=None):
        self.server.asked.append((self.headers["Host"].split(":")[0], urlsplit(self.path).path, status))
        data = body.encode()
        self.send_response(status)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(data)), **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


class ToFakeStores(httpx.HTTPTransport):
    """The crawler's https://<store>/ requests, sent over plain HTTP to the fake stores (the Host header stays)."""

    def __init__(self, port: int):
        super().__init__()
        self.port = port

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return super().handle_request(request)


@pytest.fixture
def stores(monkeypatch, no_network):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name)  # the client's own transport, not an env proxy; the socket guard still holds
    server = FakeStores()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_init = http.PoliteClient.__init__

    def init(self, *a, **k):
        real_init(self, *a, **(k | {"transport": ToFakeStores(server.server_address[1])}))

    monkeypatch.setattr(http.PoliteClient, "__init__", init)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)  # Retry-After and pacing, without the wait
    yield server
    server.shutdown()
    server.server_close()


def test_crawl_only_one_roaster_against_a_fake_store(tmp_path, monkeypatch, stores):
    lake = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))
    roasters = load_roasters()
    monkeypatch.setattr(
        registry,
        "current",
        lambda lake, *a, **k: [registry.RoasterState(r, "active", None) for r in roasters.values()],
    )
    week = connectors.week_of(cli._today())

    cli.main(["crawl", "--connector", "shopify", "--only", "onyxcoffeelab.com", "--run-date", week])
    part = connectors.partition(lake, "shopify", week)
    con = lake.connect()
    [fetch] = rows(con, part / "fetches.parquet")
    products = len(stores.onyx["products"])
    assert (fetch["roaster_id"], fetch["status"], fetch["products"], fetch["partial"]) == (
        "onyxcoffeelab.com", "ok", products, False)  # fmt: skip
    statuses = {s["status"]: s["responses"] for s in fetch["http_statuses"]}
    assert statuses == {200: 3, 429: 1} and fetch["rate_limited"] >= 1  # robots.txt, the 429, page 1, page 2
    [manifest] = rows(con, part / "run_manifests.parquet")
    assert (manifest["roaster_id"], manifest["mode"], manifest["adapter"]) == (
        "onyxcoffeelab.com",
        "only",
        "shopify",
    )
    snaps = rows(con, part / "snapshots.parquet")
    assert len(snaps) == products and {s["batch_id"] for s in snaps} == {fetch["batch_id"]}
    [end] = [r for r in control.rows(lake, week) if r["connector"] == "shopify" and r["status"] != "running"]
    assert (end["status"], end["ok"], end["errors"]) == ("done", 1, 0)
    assert json.loads(end["detail"])["only"] == "onyxcoffeelab.com"

    # An edge's challenge: a failed fetch with its block recorded, never parked for a retry, nothing listed.
    cli.main(["crawl", "--connector", "woocommerce", "--only", "anaheimcoffee.com", "--run-date", week])
    part = connectors.partition(lake, "woocommerce", week)
    [fetch] = rows(con, part / "fetches.parquet")
    assert (fetch["roaster_id"], fetch["status"], fetch["products"]) == ("anaheimcoffee.com", "error", 0)
    assert "HTTP 202" in fetch["error"]
    [block] = fetch["blocks"]
    assert (block["status"], block["path"], block["sg_captcha"]) == (
        202, "/wp-json/wc/store/v1/products", "challenge")  # fmt: skip
    assert not (part / "snapshots.parquet").exists() or not rows(con, part / "snapshots.parquet")
    assert ("anaheimcoffee.com", "/wp-json/wc/store/v1/products", 202) in stores.asked
    assert {h for h, _, _ in stores.asked} == {"onyxcoffeelab.com", "anaheimcoffee.com"}


def rows(con, path) -> list[dict]:
    cur = con.execute(f"select * from read_parquet('{path}')")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
