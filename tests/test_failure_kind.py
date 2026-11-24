"""Why a fetch failed, kept on the stored row (fetches.error starts with `[kind] `).

Run 2026-11-16: about 30 stores failed with `FetchError: RobotsUnavailable <url>` and nothing else, so a dead site, a
DNS failure, a refused connection, a TLS error and a timeout all read the same. Each kind here comes from the real
httpx/httpcore code: the crawler's own transport (netguard.guarded_transport, httpx's HTTPTransport and httpcore's
SyncBackend) with only the operating system's answer faked (the resolver, or socket.create_connection handing back a
loopback socket). The proxy case, which can't happen in the crawl (no proxy), comes from a MockTransport. Nothing is sent anywhere but to a socket this test opened on 127.0.0.1.
"""

from __future__ import annotations

import errno
import socket
import struct
from collections.abc import Iterator
from datetime import date

import httpcore
import httpx
import pyarrow.parquet as pq
import pytest

from coffeedata import http, ingest, netguard
from coffeedata.http import PoliteClient, RobotsUnavailable
from coffeedata.models import Roaster

STORE = Roaster(roaster_id="store.example", name="S", host="store.example", platform="other")
SHOP = Roaster(roaster_id="shop.example", name="S", host="shop.example", platform="shopify")
_create_connection = socket.create_connection


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "egress_ip", lambda: "203.0.113.7")
    # every name resolves to one public address; no lookup leaves the process
    monkeypatch.setattr(
        netguard,
        "_getaddrinfo",
        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))],
    )


@pytest.fixture
def loopback() -> Iterator[tuple[socket.socket, socket.socket]]:
    """A connected TCP pair on 127.0.0.1: (ours, the "server's" end)."""
    srv = socket.create_server(("127.0.0.1", 0))
    ours = _create_connection(srv.getsockname(), timeout=5)
    peer, _ = srv.accept()
    srv.close()
    yield ours, peer
    for s in (ours, peer):
        s.close()


def _connect_with(monkeypatch, outcome) -> None:
    """socket.create_connection (httpcore's SyncBackend calls it) answers with `outcome`: raised if an exception,
    else returned as the connected socket."""

    def fake(address, timeout=None, source_address=None, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(socket, "create_connection", fake)


def _real_client(**kw) -> PoliteClient:
    return PoliteClient(min_interval=0, retries=1, **kw)  # the crawler's own guarded transport


def _stored(res: ingest.Result, lake) -> dict:
    rows = ingest.rows_for([res], date(2026, 11, 23), "b", set())
    (row,) = pq.read_table(lake.write_partition("fetches", "2026-11-23", "b", rows["fetches"])).to_pylist()
    return row


def _closed_port() -> int:
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- each kind, from the real transport ------------------------------------------------------------------------------


def test_dns_failure(monkeypatch, lake):
    def no_such_name(host, port, **kw):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(netguard, "_getaddrinfo", no_such_name)
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert res.kind == "dns"
    row = _stored(res, lake)
    assert row["error"].startswith("[dns] FetchError: RobotsUnavailable https://store.example/")
    # the robots.txt message that used to be lost is kept: what failed, and the socket's own words
    assert (
        "robots.txt for store.example failed (ConnectError: [Errno -2] Name or service not known"
        in row["error"]
    )
    assert row["transport_errors"][0]["phase"] == "dns"


def test_connection_refused(monkeypatch, lake):
    port = _closed_port()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda a, timeout=None, source_address=None, **kw: _create_connection(("127.0.0.1", port), timeout),
    )
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith("[connection_refused] FetchError: RobotsUnavailable")
    assert "Connection refused" in res.error


def test_connection_reset_during_the_tls_handshake(monkeypatch, lake, loopback):
    ours, peer = loopback
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    peer.close()  # an RST, not a FIN
    _connect_with(monkeypatch, ours)
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith("[connection_reset] ")


def test_tls_error(monkeypatch, lake, loopback):
    ours, peer = loopback
    peer.sendall(
        b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
    )  # plain HTTP where TLS was expected
    _connect_with(monkeypatch, ours)
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith("[tls] FetchError: RobotsUnavailable")
    assert "SSL" in res.error


def test_connect_timeout(monkeypatch, lake):
    _connect_with(monkeypatch, TimeoutError("timed out"))
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith("[connect_timeout] FetchError: RobotsUnavailable")


def test_read_timeout(monkeypatch, lake, loopback):
    """Connected, then nothing comes back. TLS is skipped (the stream answers start_tls with itself), so the read is
    httpcore's own, on the real socket, with its real timeout."""
    ours, _peer = loopback

    class NoTLS(httpcore._backends.sync.SyncStream):
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            return self

    class Backend(httpcore.SyncBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return NoTLS(ours)

    transport = netguard.guarded_transport(httpx.Limits(), backend=Backend())
    with PoliteClient(transport=transport, min_interval=0, retries=1, timeout=0.2) as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith("[read_timeout] FetchError: RobotsUnavailable")


def test_no_local_address_is_local_address_not_the_nat(monkeypatch, lake):
    _connect_with(monkeypatch, OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"))
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith("[local_address] ")


def test_ipv4_refused_then_ipv6_unassignable_ends_as_local_address(monkeypatch):
    """The review's case: netguard tries each resolved address and re-raises the last error, so a store whose IPv4
    address refused and whose IPv6 one can't be tried (no IPv6 source address) reads local_address, which is why the
    kind doesn't name the NAT."""
    monkeypatch.setattr(
        netguard,
        "_getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", port, 0, 0)),
        ],
    )
    tried = []

    def fake(address, timeout=None, source_address=None, **kw):
        tried.append(address[0])
        if ":" in address[0]:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(socket, "create_connection", fake)
    with _real_client() as c:
        res = ingest.ingest_one(c, STORE)
    assert tried == ["8.8.8.8", "2001:4860:4860::8888"] and res.kind == "local_address"


def test_a_proxy_error_gets_no_kind():
    """The crawl never goes through a proxy (trust_env=False, netguard's transport), so a ProxyError has no kind of
    its own: no prefix, the message says it."""

    def handler(request):
        raise httpx.ProxyError("502 Bad Gateway from the proxy", request=request)

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        res = ingest.ingest_one(c, STORE)
    assert res.kind is None and ingest.stored_error(res).startswith("FetchError: RobotsUnavailable")


# --- answers that aren't robots.txt's rules, and the catalog's own failures ------------------------------------------


@pytest.mark.parametrize(
    "status, headers, kind",
    [
        (202, {"sg-captcha": "challenge"}, "challenge_202"),
        (403, {"cf-mitigated": "challenge"}, "challenge_403"),
        (429, {"retry-after": "1"}, "block_429"),
        (503, {}, "block_503"),
        (500, {}, "http_500"),
    ],
)
def test_robots_answered_with_a_challenge_a_block_or_an_error(status, headers, kind, lake):
    def handler(request):
        return httpx.Response(status, headers=headers, text="no")

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        res = ingest.ingest_one(c, STORE)
    assert _stored(res, lake)["error"].startswith(f"[{kind}] ")
    assert f"robots.txt for store.example answered {status}" in res.error


@pytest.mark.parametrize(
    "answer, kind",
    [
        (httpx.Response(429, text="slow down"), "block_429"),
        (httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<html>"), "challenge_403"),
        (httpx.Response(404, text="no"), "http_404"),
        # a 200 that isn't the catalog: no kind fits, so none is guessed (the message says "not JSON")
        (httpx.Response(200, headers={"content-type": "text/html"}, text="<html>"), None),
        (socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"), "dns"),
    ],
)
def test_a_catalog_request_that_fails_after_robots_txt_answered(answer, kind, lake):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if isinstance(answer, BaseException):
            try:
                raise answer
            except OSError as e:
                raise httpx.ConnectError(str(e), request=request) from e
        return answer

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        res = ingest.ingest_one(c, SHOP)
    assert res.status == "error" and res.kind == kind
    error = _stored(res, lake)["error"]
    assert error.startswith(f"[{kind}] ") if kind else not error.startswith("[")


def test_a_host_remembered_as_down_fails_fast_with_the_same_kind(monkeypatch):
    """A second fetch of the host within ROBOTS_RETRY_AFTER sends nothing (`(recently)`), and still says why."""
    _connect_with(monkeypatch, ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    with _real_client() as c:
        first = ingest.ingest_one(c, STORE)
        second = ingest.ingest_one(c, STORE)
    assert first.kind == second.kind == "connection_refused"
    assert "(recently)" in second.error and second.stats.requests == 0


def test_the_kind_never_changes_what_the_crawler_does(monkeypatch):
    """Recording only: the error's flags (unreachable, status, challenge, wait) are as before."""
    _connect_with(monkeypatch, ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    with _real_client() as c, pytest.raises(RobotsUnavailable) as caught:
        c.get("https://store.example/products.json")
    e = caught.value
    assert (e.kind, e.unreachable, e.status, e.challenge, e.wait) == (
        "connection_refused",
        True,
        None,
        False,
        None,
    )


def test_an_ok_or_partial_fetch_gets_no_kind():
    ok = ingest.Result(STORE, ingest.datetime.now(ingest.UTC), partial=True, partial_reason="partial: page 3")
    assert ingest.stored_error(ok) == "partial: page 3"
    quarantined = ingest.Result(STORE, ingest.datetime.now(ingest.UTC), error="in flight", quarantined=True)
    assert ingest.stored_error(quarantined) == "in flight"
    unavailable = ingest.Result(
        STORE, ingest.datetime.now(ingest.UTC), error="x", unavailable=True, kind="http_404"
    )
    assert ingest.stored_error(unavailable) == "x"  # only a failed fetch is tagged


def test_the_stored_error_stays_within_300_characters():
    res = ingest.Result(STORE, ingest.datetime.now(ingest.UTC), error="x" * 300, kind="connection_refused")
    stored = ingest.stored_error(res)
    assert len(stored) == 300 and stored.startswith("[connection_refused] x")


def test_the_kind_is_countable_with_one_regexp(lake):
    """The query the PR's description and the contract's docs give."""
    import duckdb

    got = duckdb.sql(
        "select regexp_extract(error, '^\\[([a-z0-9_]+)\\] ', 1) as kind from (values ('[dns] FetchError: a'), "
        "('[block_429] FetchError: b'), ('FetchError: [Errno 111] c'), (null)) t(error) order by all"
    ).fetchall()
    assert [k for (k,) in got] == ["", "block_429", "dns", None]


# --- review fixes: deadline, disallowed, too_large first, and recording never decides --------------------------------


def test_a_deadline_stop_with_nothing_read_is_deadline():
    with PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)), min_interval=0, deadline_s=-1
    ) as c:
        res = ingest.ingest_one(c, SHOP)
    assert res.status == "error" and res.deadline and res.kind == "deadline"


def test_a_robots_disallow_is_disallowed():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /products.json\n")
        return httpx.Response(200, json={"products": []})

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        res = ingest.ingest_one(c, SHOP)
    assert res.status == "error" and res.kind == "disallowed"
    assert ingest.stored_error(res).startswith("[disallowed] ")


def test_an_oversized_503_is_too_large():
    e = http.ResponseTooLarge("response over 32 MiB: https://shop.example/products.json", status=503)
    assert http.failure_kind(http.FetchError("x")) is None
    assert http.failure_kind(e) == "too_large"


def test_a_failure_kind_that_raises_changes_nothing_but_the_kind(monkeypatch):
    """Recording only: the kind is set after every flag that decides, and a kind that can't be read is None."""

    def handler(request):
        return httpx.Response(429, headers={"retry-after": "5"}, text="slow down")

    def boom(e):
        raise RuntimeError("unreadable chain")

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        before = ingest.ingest_one(c, STORE)
    monkeypatch.setattr(ingest, "failure_kind", boom)
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        after = ingest.ingest_one(c, STORE)
    assert before.kind == "block_429" and after.kind is None
    flags = ("error", "deferrable", "wait", "blocked", "unavailable", "deadline")
    assert [getattr(after, f) for f in flags] == [getattr(before, f) for f in flags]
