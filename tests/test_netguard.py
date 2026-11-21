"""Where the crawler may connect (security review 2026-11-21, F1, F2, F7, F8): only to globally routable addresses,
pinned to the address it vetted, never to an IP literal; robots.txt only from the host's own site; sitemaps and
product pages only on the store's site; WooCommerce variation ids digits only; a total time limit per request.

Every host here is made up, and every name is resolved by a mocked resolver: nothing is looked up or fetched."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta

import httpcore
import httpx
import pytest
import respx

from coffeedata import http, netguard, registry, sitemap
from coffeedata.adapters import generic, woocommerce
from coffeedata.http import FetchError, PartialCatalog, PoliteClient, RobotsUnavailable
from coffeedata.models import Roaster
from coffeedata.netguard import (
    GuardedBackend,
    OffSiteRedirect,
    RefusedAddress,
    RequestTooSlow,
    address_allowed,
    guarded_transport,
    ip_literal,
    on_site,
    vetted_addresses,
)

# Globally routable addresses (used as data only: the fake backend never opens a socket).
PUBLIC_V4, PUBLIC_V4_B, PUBLIC_V6 = "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"


# --- hosts and sites -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["10.0.0.5", "127.0.0.1", "[::1]", "::1", "169.254.169.254", "10.1", "0x7f.1", "2130706433", "1.2.3.4."],
)
def test_ip_literals_are_recognised(host):
    assert ip_literal(host)


@pytest.mark.parametrize(
    "host", ["roaster.example", "www.roaster.example", "1password.example", "a.b.c.example"]
)
def test_names_are_not_ip_literals(host):
    assert not ip_literal(host)


@pytest.mark.parametrize(
    ("host", "site", "ok"),
    [
        ("roaster.example", "roaster.example", True),
        ("www.roaster.example", "roaster.example", True),
        ("roaster.example", "www.roaster.example", True),
        ("shop.roaster.example", "roaster.example", True),
        ("ROASTER.example.", "roaster.example", True),
        ("other.example", "roaster.example", False),
        ("roaster.example.evil.example", "roaster.example", False),
        ("evilroaster.example", "roaster.example", False),
        ("roaster.example", "shop.roaster.example", False),  # the parent isn't the store's
        ("b.myshop.example", "a.myshop.example", False),  # another tenant of a shared host
        ("10.0.0.5", "10.0.0.5", False),
        (None, "roaster.example", False),
    ],
)
def test_on_site(host, site, ok):
    assert on_site(host, site) is ok


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
     "192.0.2.1", "224.0.0.1", "::1", "fc00::1", "fe80::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "not an ip"],
)  # fmt: skip
def test_addresses_that_are_not_globally_routable_are_refused(address):
    assert not address_allowed(address)


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "::ffff:8.8.8.8"])
def test_global_addresses_are_allowed(address):
    assert address_allowed(address)


@pytest.mark.parametrize("domain", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "10.1"])
def test_the_registry_refuses_ip_literals(domain):
    assert not registry.is_storefront_host(domain)
    assert registry.is_storefront_host("roaster.example")


# --- the resolver and the backend ------------------------------------------------------------------------------------


@pytest.fixture
def dns(monkeypatch):
    """A made-up DNS: name -> addresses. A name not listed fails like a real lookup miss."""
    table: dict[str, list[str]] = {}
    asked: list[str] = []

    def getaddrinfo(host, port, *a, **k):
        asked.append(host)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (socket.AF_INET6 if ":" in a_ else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a_, port))
            for a_ in table[host]
        ]

    monkeypatch.setattr(netguard, "_getaddrinfo", getaddrinfo)
    table["asked"] = asked  # type: ignore[assignment]
    return table


class FakeStream(httpcore.NetworkStream):
    """A connection that answers every request with `reply` (raw HTTP/1.1 bytes)."""

    def __init__(self, reply: bytes) -> None:
        self.reply, self.sent = reply, b""

    def read(self, max_bytes, timeout=None):
        out, self.reply = self.reply[:max_bytes], self.reply[max_bytes:]
        return out

    def write(self, buffer, timeout=None):
        self.sent += buffer

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni = server_hostname
        return self

    def get_extra_info(self, info):
        return None


class RecordingBackend(httpcore.NetworkBackend):
    """Records each address it is asked to connect to; `down` addresses fail to connect; `replies` maps an address
    to what its server answers."""

    def __init__(self, replies: dict[str, bytes] | None = None, down: set[str] | None = None) -> None:
        self.connects: list[str] = []
        self.replies, self.down = replies or {}, down or set()
        self.streams: list[FakeStream] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append(host)
        if host in self.down:
            raise httpcore.ConnectError("refused")
        s = FakeStream(self.replies.get(host, b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"))
        self.streams.append(s)
        return s

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("never")

    def sleep(self, seconds):
        pass


def test_vetted_addresses_needs_every_address_global(dns):
    dns["roaster.example"] = [PUBLIC_V4, PUBLIC_V6]
    assert vetted_addresses("roaster.example", 443) == [PUBLIC_V4, PUBLIC_V6]
    dns["mixed.example"] = [PUBLIC_V4, "10.0.0.5"]
    with pytest.raises(RefusedAddress):
        vetted_addresses("mixed.example", 443)
    with pytest.raises(RefusedAddress):
        vetted_addresses("10.0.0.5", 443)
    with pytest.raises(httpcore.ConnectError):  # a lookup miss stays a connect error (phase dns)
        vetted_addresses("nowhere.example", 443)


def test_the_backend_connects_to_the_vetted_address_not_the_name(dns):
    dns["roaster.example"] = [PUBLIC_V4, PUBLIC_V4_B]
    inner = RecordingBackend(down={PUBLIC_V4})
    stream = GuardedBackend(inner).connect_tcp("roaster.example", 443)
    assert inner.connects == [
        PUBLIC_V4,
        PUBLIC_V4_B,
    ]  # the first didn't answer: the next vetted one, never the name
    assert stream is not None
    assert dns["asked"] == ["roaster.example"]  # resolved once: no second answer can swap in another address


def test_the_backend_never_connects_to_a_refused_address(dns):
    dns["rebound.example"] = ["127.0.0.1"]
    inner = RecordingBackend()
    with pytest.raises(RefusedAddress):
        GuardedBackend(inner).connect_tcp("rebound.example", 443)
    assert inner.connects == []


def test_the_crawlers_default_transport_is_guarded_and_ignores_env_proxies():
    with PoliteClient(min_interval=0) as c:
        pool = c._client._transport._pool
        assert isinstance(pool._network_backend, GuardedBackend)
        assert c._client.trust_env is False


def _ok(body: str = "", extra: str = "") -> bytes:
    data = body.encode()
    return f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\n{extra}\r\n".encode() + data


def test_a_redirect_hop_to_a_private_address_is_refused_through_the_real_transport(dns):
    """End to end through httpx's transport: robots.txt and the page come from the vetted public address (TLS named
    for the host), and the redirect's hop to a name that resolves to 10.0.0.5 never connects."""
    dns["roaster.example"] = [PUBLIC_V4]
    dns["internal.example"] = ["10.0.0.5"]
    reply = _ok("User-agent: *\nAllow: /\n") + (
        b"HTTP/1.1 302 Found\r\nLocation: https://internal.example/admin\r\nContent-Length: 0\r\n\r\n"
    )
    inner = RecordingBackend({PUBLIC_V4: reply})
    transport = guarded_transport(httpx.Limits(), backend=inner)
    with (
        PoliteClient(min_interval=0, retries=1, transport=transport) as c,
        pytest.raises(RobotsUnavailable) as e,
    ):
        c.get(
            "https://roaster.example/products"
        )  # the hop's own robots.txt is the first request it would send
    assert "phase refused_address" in str(e.value)
    assert inner.connects == [PUBLIC_V4]
    assert inner.streams[0].sni == "roaster.example"
    assert b"Host: roaster.example" in inner.streams[0].sent


def test_robots_txt_on_a_private_address_is_unavailable_never_no_rules(dns):
    dns["roaster.example"] = ["192.168.0.10"]
    inner = RecordingBackend()
    with (
        PoliteClient(
            min_interval=0, retries=1, transport=guarded_transport(httpx.Limits(), backend=inner)
        ) as c,
        pytest.raises(RobotsUnavailable) as e,
    ):
        c.get("https://roaster.example/products")
    assert inner.connects == []
    assert not e.value.unreachable  # a refusal, not a host that is down


# --- IP literals and robots.txt redirects (respx: no transport at all) ---------------------------------------------


NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)


def test_an_ip_literal_host_is_refused_before_anything_is_sent():
    with respx.mock(assert_all_called=False) as m, PoliteClient(min_interval=0, retries=1) as c:
        route = m.get(url__regex=r".*").mock(return_value=httpx.Response(200))
        for url in ("https://10.0.0.5/products.json", "http://[::1]/x", "https://169.254.169.254/x"):
            with pytest.raises(RefusedAddress):
                c.get(url)
        assert not route.called


def test_a_page_redirect_to_an_ip_literal_is_refused():
    with respx.mock(assert_all_called=False) as m, PoliteClient(min_interval=0, retries=1) as c:
        m.get("https://roaster.example/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://roaster.example/p").mock(
            return_value=httpx.Response(302, headers={"location": "http://10.0.0.5:8080/admin"})
        )
        inner = m.get(url__regex=r"https?://10\.0\.0\.5.*").mock(return_value=httpx.Response(200))
        with pytest.raises(RefusedAddress):
            c.get("https://roaster.example/p")
        assert not inner.called


@pytest.mark.parametrize(
    "target",
    ["https://elsewhere.example/robots.txt", "https://roaster.example.evil.example/robots.txt",
     "http://10.0.0.5:8080/admin"],
)  # fmt: skip
@pytest.mark.parametrize("stored_copy", [False, True])
def test_a_robots_redirect_off_the_site_is_robots_unavailable(target, stored_copy):
    """Never read as "no robots.txt" (which would allow everything), and no stored copy stands in: the host's rules
    only ever come from its own site."""
    copies = {"roaster.example": {"fetched_at": NOW - timedelta(days=1), "status": 404, "body": None}}
    with (
        respx.mock(assert_all_called=False) as m,
        PoliteClient(
            min_interval=0, retries=1, robots_copies=copies if stored_copy else None, clock=lambda: NOW
        ) as c,
    ):
        m.get("https://roaster.example/robots.txt").mock(
            return_value=httpx.Response(301, headers={"location": target})
        )
        away = m.get(url__regex=r"https?://(elsewhere|roaster\.example\.evil|10\.).*").mock(
            return_value=httpx.Response(404)
        )
        page = m.get("https://roaster.example/products.json").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(RobotsUnavailable) as e:
            c.get("https://roaster.example/products.json")
        assert not away.called and not page.called
        assert "refused" in str(e.value)
        with pytest.raises(RobotsUnavailable):  # remembered: not asked again at once
            c.get("https://roaster.example/products.json")
        assert m.routes[0].call_count == 1


def test_a_robots_redirect_within_the_site_is_followed():
    with respx.mock(assert_all_called=False) as m, PoliteClient(min_interval=0, retries=1) as c:
        m.get("https://roaster.example/robots.txt").mock(
            return_value=httpx.Response(301, headers={"location": "https://www.roaster.example/robots.txt"})
        )
        m.get("https://www.roaster.example/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        )
        m.get("https://roaster.example/products.json").mock(return_value=httpx.Response(200, json={}))
        assert c.get("https://roaster.example/products.json").status_code == 200
        with pytest.raises(http.Blocked):
            c.get("https://roaster.example/private/x")


def test_off_site_redirect_and_refused_address_are_http_errors_with_their_own_phase():
    assert issubclass(OffSiteRedirect, httpx.HTTPError) and issubclass(RefusedAddress, httpx.HTTPError)
    assert http.transport_phase(RefusedAddress("x")) == "refused_address"
    assert http.transport_phase(OffSiteRedirect("x")) == "off_site_redirect"
    assert http.transport_phase(RequestTooSlow("x")) == "total_timeout"


# --- sitemaps and product pages stay on the store's site -------------------------------------------------------------


def _urlset(urls) -> str:
    return "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"


def _index(urls) -> str:
    return "<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls) + "</sitemapindex>"


def _client(handler, **kw) -> PoliteClient:
    def wrapped(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(req)

    return PoliteClient(transport=httpx.MockTransport(wrapped), min_interval=0, retries=1, **kw)


def test_sitemap_keeps_only_urls_on_the_stores_site():
    seen: list[str] = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=_urlset([
            "https://roaster.example/products/a", "https://www.roaster.example/products/b",
            "https://other.example/products/c", "https://10.0.0.5/products/d", "ftp://roaster.example/products/e",
            "https://[bad/products/f",
        ]))  # fmt: skip

    with _client(handler) as c:
        urls = sitemap.sitemap_urls(c, "roaster.example")
    assert urls == ["https://roaster.example/products/a", "https://www.roaster.example/products/b"]
    assert seen == ["https://roaster.example/sitemap.xml"]


def test_a_sub_sitemap_off_the_site_is_not_read_and_leaves_the_list_incomplete():
    seen: list[str] = []

    def handler(req):
        seen.append(req.url.host)
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_index([
                "https://roaster.example/product-sitemap.xml", "https://other.example/product-sitemap.xml",
                "https://127.0.0.1/sitemap.xml",
            ]))  # fmt: skip
        return httpx.Response(200, text=_urlset(["https://roaster.example/products/a"]))

    unread: list[str] = []
    with _client(handler) as c:
        assert sitemap.sitemap_urls(c, "roaster.example", unread) == ["https://roaster.example/products/a"]
    assert set(seen) == {"roaster.example"}
    assert unread == ["2 sub-sitemaps off the store's site"]


def test_sub_sitemaps_past_the_cap_leave_the_list_incomplete(monkeypatch):
    monkeypatch.setattr(sitemap, "MAX_SUB_SITEMAPS", 3)
    subs = [f"https://roaster.example/sitemap-{n}.xml" for n in range(5)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_index(subs))
        return httpx.Response(200, text=_urlset([f"https://roaster.example/products{req.url.path}"]))

    unread: list[str] = []
    with _client(handler) as c:
        assert len(sitemap.sitemap_urls(c, "roaster.example", unread)) == 3
    assert unread == ["2 sub-sitemaps past the cap of 3"]


def test_sitemap_urls_past_the_cap_leave_the_list_incomplete(monkeypatch):
    monkeypatch.setattr(sitemap, "MAX_SITEMAP_URLS", 4)
    subs = [f"https://roaster.example/sitemap-{n}.xml" for n in range(3)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_index(subs))
        return httpx.Response(200, text=_urlset([f"https://roaster.example/products{req.url.path}/{i}" for i in range(3)]))  # fmt: skip

    unread: list[str] = []
    with _client(handler) as c:
        assert len(sitemap.sitemap_urls(c, "roaster.example", unread)) == 4
    assert unread == [
        "2 URLs of https://roaster.example/sitemap-1.xml past the cap of 4 URLs",
        "1 sub-sitemaps past 4 URLs",
    ]


def _product_page(i: int) -> str:
    ld = f'{{"@type": "Product", "name": "Coffee {i}", "sku": "s{i}", "offers": {{"price": "18"}}}}'
    return f'<script type="application/ld+json">{ld}</script>'


def _roaster(host: str = "roaster.example") -> Roaster:
    return Roaster(roaster_id=host, name=host, host=host, platform="custom")


def test_generic_reads_no_product_page_off_the_site():
    seen: list[str] = []

    def handler(req):
        seen.append(req.url.host)
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(
                ["https://roaster.example/products/c1", "https://other.example/products/c2"]
            ))  # fmt: skip
        return httpx.Response(200, text=_product_page(int(req.url.path.rsplit("c", 1)[1])))

    with _client(handler) as c:
        assert [i.product_id for i in generic.fetch(c, _roaster())] == ["s1"]
    assert "other.example" not in seen


def test_generic_product_pages_past_the_cap_are_stored_as_partial(monkeypatch):
    monkeypatch.setattr(generic, "MAX_PRODUCT_PAGES", 2)
    urls = [f"https://roaster.example/products/c{i}" for i in range(4)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(urls))
        return httpx.Response(200, text=_product_page(int(req.url.path.rsplit("c", 1)[1])))

    with _client(handler) as c, pytest.raises(PartialCatalog) as e:
        generic.fetch(c, _roaster())
    assert sorted(i.product_id for i in e.value.items) == ["s0", "s1"]
    assert "2 product pages past the cap of 2" in str(e.value)


# --- WooCommerce variation ids ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("vid", ["../../other?x=", "12/../13", "1e3", "١٢", "", "12 "])
def test_a_variation_id_that_isnt_digits_is_never_put_in_a_path(vid):
    seen: list[str] = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={})

    with _client(handler) as c:  # that size is dropped (as for a 404); the rest of the store is read
        assert woocommerce._lookup(c, "https://roaster.example/wp-json/wc/store/v1/products", vid) is None
    assert seen == []


def test_a_digit_variation_id_is_looked_up():
    def handler(req):
        return httpx.Response(200, json={"id": 123, "prices": {}})

    with _client(handler) as c:
        assert woocommerce._lookup(c, "https://roaster.example/wp-json/wc/store/v1/products", "123") == {
            "id": 123,
            "prices": {},
        }


# --- the total time limit --------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


def test_a_slow_drip_response_stops_at_the_total_limit_and_is_not_retried(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(netguard.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(http.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    sent: list[str] = []

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                clock.t += 5.0  # a byte every five seconds: each read is well inside the per-read timeout
                yield b"x"

    def handler(req):
        sent.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, stream=Drip())

    with (
        PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=3) as c,
        c.track() as stats,
        pytest.raises(RequestTooSlow),
    ):
        c.get("https://roaster.example/slow")
    assert sent.count("/slow") == 1  # never retried
    assert stats.transport_errors[0]["phase"] == "total_timeout"


def test_the_guarded_stream_bounds_every_read_by_the_total_limit(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(netguard.time, "monotonic", clock.monotonic)
    asked: list[float | None] = []

    class Inner(FakeStream):
        def read(self, max_bytes, timeout=None):
            asked.append(timeout)
            clock.t += 50
            return b"x"

    stream = netguard._DeadlineStream(Inner(b""))
    with netguard.request_deadline(120):
        stream.read(1, timeout=15)
        stream.read(1, timeout=15)
        stream.read(1, timeout=15)  # 100 s gone: at most 20 s left, so it waits 15
        with pytest.raises(httpcore.ReadTimeout) as e:
            stream.read(1, timeout=15)  # 150 s gone
    assert asked == [15, 15, 15]
    assert netguard.too_slow(e.value)
    stream.read(1, timeout=15)  # outside a request's block: no total limit
    assert asked[-1] == 15


# --- the review round (#211) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::a00:5", "64:ff9b::7f00:1", "64:ff9b:1::a9fe:a9fe", "::a00:5", "::ffff:0:a00:5", "2002:a00:5::1",
     "fec0::1", "::", "::1"],
)  # fmt: skip
def test_ipv6_forms_that_carry_a_private_ipv4_address_are_refused(address):
    assert not address_allowed(address)


@pytest.mark.parametrize("address", ["64:ff9b::808:808", "2002:808:808::1"])
def test_ipv6_forms_that_carry_a_global_ipv4_address_are_allowed(address):
    assert address_allowed(address)


def test_a_flat_sitemap_with_every_url_off_the_site_fails_instead_of_an_empty_catalog():
    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=_urlset(["https://newhome.example/products/a", "https://newhome.example/products/b"]),
            )
        return httpx.Response(200, text=_product_page(1))

    with _client(handler) as c:
        with pytest.raises(FetchError, match="off the store's site"):
            sitemap.sitemap_urls(c, "roaster.example")
        with pytest.raises(FetchError):  # generic: a failed fetch, never [] stored as the whole catalog
            generic.fetch(c, _roaster())


def test_a_sitemap_index_whose_urls_are_all_off_the_site_fails():
    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_index(["https://roaster.example/product-sitemap.xml"]))
        return httpx.Response(200, text=_urlset(["https://newhome.example/products/a"]))

    with _client(handler) as c, pytest.raises(FetchError, match="off the store's site"):
        sitemap.sitemap_urls(c, "roaster.example", [])


def test_a_flat_sitemap_past_the_url_cap_is_partial(monkeypatch):
    monkeypatch.setattr(sitemap, "MAX_SITEMAP_URLS", 2)

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(
                200, text=_urlset([f"https://roaster.example/products/c{i}" for i in range(3)])
            )
        return httpx.Response(200, text=_product_page(int(req.url.path.rsplit("c", 1)[1])))

    unread: list[str] = []
    with _client(handler) as c:
        assert len(sitemap.sitemap_urls(c, "roaster.example", unread)) == 2
        assert unread == ["1 sitemap URLs past the cap of 2 URLs"]
        with pytest.raises(PartialCatalog):
            generic.fetch(c, _roaster())


def test_the_total_limit_running_out_while_connecting_is_ours_not_the_hosts(dns, monkeypatch):
    """Not retried, recorded as total_timeout, and robots.txt is never "unreachable" for it: no stored copy."""
    dns["roaster.example"] = [PUBLIC_V4]
    clock = Clock()
    monkeypatch.setattr(netguard.time, "monotonic", clock.monotonic)

    class Slow(RecordingBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            self.connects.append((host, timeout))
            clock.t += timeout or 0
            raise httpcore.ConnectTimeout("timed out")

    inner = Slow()
    with netguard.request_deadline(5), pytest.raises(httpcore.ConnectTimeout) as e:
        GuardedBackend(inner).connect_tcp("roaster.example", 443, timeout=15)
    assert netguard.ran_out_connecting(e.value) and netguard.too_slow(e.value)
    assert inner.connects == [(PUBLIC_V4, 5)]
    # a plain connect timeout (within the total) is still the host's
    with netguard.request_deadline(120), pytest.raises(httpcore.ConnectTimeout) as e:
        GuardedBackend(Slow()).connect_tcp("roaster.example", 443, timeout=15)
    assert not netguard.ran_out_connecting(e.value)

    copies = {"roaster.example": {"fetched_at": NOW - timedelta(days=1), "status": 404, "body": None}}
    transport = guarded_transport(httpx.Limits(), backend=Slow())
    client = PoliteClient(
        min_interval=0,
        retries=3,
        robots_copies=copies,
        clock=lambda: NOW,
        max_request_s=5,
        transport=transport,
    )
    with client as c, c.track() as stats, pytest.raises(RobotsUnavailable) as r:
        c.get("https://roaster.example/products.json")
    assert not r.value.unreachable
    assert len(stats.transport_errors) == 1 and stats.transport_errors[0]["phase"] == "total_timeout"
