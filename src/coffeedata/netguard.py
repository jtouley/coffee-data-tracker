"""Where the crawler may connect, and for how long (security review 2026-11-21, F1, F2 and F8).

A roaster's domain comes from open data anyone can edit (OpenStreetMap's `website` tag, Overture), so the crawler
treats every host it is pointed at, and every redirect, as untrusted:

- **Addresses.** A name is resolved once per connection, and the connection is refused unless every address it
  resolves to is globally routable (`ipaddress.ip_address(a).is_global`, an IPv4-mapped IPv6 address judged as its
  IPv4 one). The connection is then made to that vetted address itself, never to the name again, so a second DNS
  answer can't swap in another address (DNS rebinding). TLS still checks the certificate against the name: httpcore
  sends the request's host as SNI and verifies it, whatever address the socket went to. The check runs per
  connection, so every redirect hop to a new host is checked as it is made.
- **IP literals.** A URL whose host is an address (`10.0.0.5`, `[::1]`, or a numeric form such as `1.0x7f`) is
  refused before anything is sent: a roaster has a name.
- **Sites.** `on_site` says whether a URL's host belongs to a roaster's site: the roaster's host without a leading
  `www.`, or a subdomain of it. Sitemaps and product pages stay on it, and so does a robots.txt redirect.
- **Time.** Each request has a total time limit (MAX_REQUEST_S), not just a per-read one, so a server that sends a
  byte every few seconds can't hold a crawl worker.

httpx has no public setting for the network layer, so `guarded_transport` builds httpx's own transport and gives its
connection pool (httpcore's, whose `network_backend` is public in httpcore) the guarded backend. If a dependency
upgrade moves that attribute, building the transport fails rather than running unguarded.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpcore
import httpx

# The longest one request may take in total, from connecting to its last byte (F8). The largest file the crawler
# reads is a sitemap of up to 64 MiB (sitemap.SITEMAP_MAX_BYTES), which takes well under this on any working server.
MAX_REQUEST_S = 120.0

# Resolution, replaceable in tests (a made-up host and a mocked resolver: tests never look up a real name).
_getaddrinfo = socket.getaddrinfo


class RefusedAddress(httpx.HTTPError):
    """A host the crawler never connects to: an IP literal, or a name that resolves to an address that isn't globally
    routable (private, loopback, link-local, shared, reserved...). Never retried, and never read as a host being down:
    a robots.txt fetch refused this way leaves the host without rules, so nothing is fetched from it (no stored copy
    stands in)."""


class OffSiteRedirect(httpx.HTTPError):
    """robots.txt redirected away from the host's site (on_site). The rules for a host only ever come from the host's
    own site, so this is robots.txt unavailable (nothing is fetched), never "no robots.txt"."""


class RequestTooSlow(httpx.ReadTimeout):
    """The request passed MAX_REQUEST_S in total. Not retried: a server this slow would hold the worker again."""


# --- hosts and sites -------------------------------------------------------------------------------------------------

_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$", re.I)


def ip_literal(host: str | None) -> bool:
    """The host is an address, not a name: a dotted or IPv6 address, or any all-numeric form the resolver would read
    as one (`10.1`, `0x7f.1`, `2130706433`): no top-level domain is numeric or starts with 0x."""
    if not host:
        return False
    h = host.strip().strip("[]").rstrip(".")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return bool(_NUMERIC_LABEL.match(h.rsplit(".", 1)[-1]))


def _site(host: str) -> str:
    return re.sub(r"^www\d?\.", "", host.strip().lower().rstrip("."))


def on_site(host: str | None, site_host: str) -> bool:
    """`host` is on `site_host`'s site: the same host, or a subdomain of it, either side without a leading `www.`.
    So www.r.com, r.com and shop.r.com are on r.com's site (and on www.r.com's), but other.com, r.com.evil.net and,
    for a store at shop.r.com, r.com itself are not. Stricter than "the same registrable domain" (no public suffix
    list is needed, and a store on a shared host such as name.myshopify.com keeps to its own subdomain)."""
    if not host or ip_literal(host):
        return False
    h, s = _site(host), _site(site_host)
    return bool(s) and (h == s or h.endswith("." + s))


# --- addresses -------------------------------------------------------------------------------------------------------


def address_allowed(address: str) -> bool:
    """A globally routable address. An IPv4-mapped IPv6 address (::ffff:10.0.0.1) is judged as its IPv4 address."""
    try:
        a = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def vetted_addresses(host: str, port: int) -> list[str]:
    """The addresses to connect to for `host`, in the resolver's order: every address it resolves to, only when all of
    them are globally routable. RefusedAddress for an IP literal or any other address; httpcore.ConnectError when the
    name doesn't resolve (a DNS failure, as before)."""
    if ip_literal(host):
        raise RefusedAddress("refused: an IP address, not a host name")
    try:
        infos = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise httpcore.ConnectError(str(e)) from e
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if not address_allowed(addr):
            raise RefusedAddress("refused: the host resolves to an address that isn't globally routable")
        if addr not in out:
            out.append(addr)
    if not out:
        raise httpcore.ConnectError(f"no address for {host}")
    return out


# --- the total time limit --------------------------------------------------------------------------------------------

_deadline = threading.local()


@contextlib.contextmanager
def request_deadline(seconds: float | None) -> Iterator[None]:
    """This thread's requests inside the block end by `seconds` from now (an httpx client is synchronous: every read
    and write of a request happens on the thread that sent it)."""
    prev = getattr(_deadline, "at", None)
    _deadline.at = None if seconds is None else time.monotonic() + seconds
    try:
        yield
    finally:
        _deadline.at = prev


def remaining() -> float | None:
    at = getattr(_deadline, "at", None)
    return None if at is None else at - time.monotonic()


def _bounded(timeout: float | None, kind: type[Exception]) -> float | None:
    left = remaining()
    if left is None:
        return timeout
    if left <= 0:
        raise kind(f"the request's total time limit ({MAX_REQUEST_S:.0f} s) passed")
    return left if timeout is None else min(timeout, left)


class _TotalTimeout(httpcore.ReadTimeout):
    """httpcore's ReadTimeout for the total limit (httpx raises it as its ReadTimeout, caused by this one)."""


class _TotalWriteTimeout(httpcore.WriteTimeout):
    pass


def too_slow(e: BaseException | None) -> bool:
    """The error is the total time limit's (RequestTooSlow, or the stream's, behind httpx's ReadTimeout)."""
    seen: set[int] = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, RequestTooSlow | _TotalTimeout | _TotalWriteTimeout):
            return True
        e = e.__cause__ or e.__context__
    return False


class _DeadlineStream(httpcore.NetworkStream):
    """A connection whose every read and write waits at most until the request's total time limit."""

    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, _bounded(timeout, _TotalTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, _bounded(timeout, _TotalWriteTimeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None):
        return _DeadlineStream(
            self._inner.start_tls(ssl_context, server_hostname, _bounded(timeout, _TotalTimeout))
        )

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


# --- the backend -----------------------------------------------------------------------------------------------------


class GuardedBackend(httpcore.NetworkBackend):
    """httpcore's network backend with the address check: resolve, vet every address, then connect to a vetted
    address (the next one if it can't be reached), never to the name. Unix sockets are refused."""

    def __init__(self, inner: httpcore.NetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        last: Exception | None = None
        for address in vetted_addresses(host, port):
            try:
                stream = self._inner.connect_tcp(
                    address, port, _bounded(timeout, httpcore.ConnectTimeout), local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
                continue
            return _DeadlineStream(stream)
        assert last is not None  # vetted_addresses never returns an empty list
        raise last

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None):
        raise RefusedAddress("refused: a unix socket")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def guarded_transport(
    limits: httpx.Limits, backend: httpcore.NetworkBackend | None = None
) -> httpx.HTTPTransport:
    """httpx's own HTTP transport (its TLS settings, limits and error mapping), with GuardedBackend underneath.
    `backend`: the backend GuardedBackend connects with (tests pass one that records what it was asked)."""
    transport = httpx.HTTPTransport(limits=limits)
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.ConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpx's transport changed: the crawler's address guard can't be installed")
    pool._network_backend = GuardedBackend(backend)
    return transport
