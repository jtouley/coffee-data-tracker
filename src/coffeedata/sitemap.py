"""Read a site's sitemap.xml (following one level of sitemap index)."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from .http import (
    BLOCK_STATUSES,
    Blocked,
    DeadlineReached,
    FetchError,
    PoliteClient,
    block_status,
    challenged,
    is_challenge,
    wrapped_message,
)
from .netguard import on_site

log = logging.getLogger(__name__)

# Matched on the body's bytes, lazily: a sitemap can be tens of MB, and decoding it whole (the old findall over r.text)
# held several copies of it per worker. Every URL on the store's own site is kept (netguard.on_site: a sitemap is
# the store's word about its own pages, never a list of other hosts to fetch). The old 5,000 URLs and 5 sub-sitemaps
# cut large catalogs; the caps below are far above any store's (the largest generic catalog read so far had 533
# products and 596 pages), and a sitemap past one is incomplete (`unread`: stored as partial), never cut silently.
_LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)
# The sitemap protocol allows 50 MB (52,428,800 bytes) uncompressed per file, above the client's usual response cap
# (http.MAX_RESPONSE_BYTES); sitemap requests get this one instead. A larger file is out of spec: ResponseTooLarge.
SITEMAP_MAX_BYTES = 64 * 2**20
MAX_SUB_SITEMAPS = 1_000  # sub-sitemaps read from one index
MAX_SITEMAP_URLS = 500_000  # URLs kept from one store's sitemaps (the protocol allows 50,000 per file)


def _locs(body: bytes) -> list[str]:
    return [m.group(1).decode("utf-8", "replace") for m in _LOC.finditer(body)]


def _on_site(url: str, host: str) -> bool:
    try:
        u = urlsplit(url)
        return u.scheme in ("http", "https") and on_site(u.hostname, host)
    except ValueError:  # a malformed <loc> (a bad IPv6 bracket, a bad port)
        return False


def _site_locs(body: bytes, host: str, what: str) -> tuple[list[str], int]:
    """The <loc>s on the store's own site, and how many others were dropped (counted in the log)."""
    locs = _locs(body)
    kept = [u for u in locs if _on_site(u, host)]
    if len(kept) < len(locs):
        log.warning("%s: %d %s off the store's site, skipped", host, len(locs) - len(kept), what)
    return kept, len(locs) - len(kept)


def _all_off_site(host: str, dropped: int) -> FetchError:
    # Nothing on the store's own site: never an empty catalog (that would delist every product), a failed fetch.
    return FetchError(f"every sitemap URL ({dropped}) is off the store's site {host}")


def _capped(urls: list[str], room: int, host: str, unread: list[str] | None, what: str) -> list[str]:
    """urls cut to `room`; a cut is noted in `unread`, so the fetch is partial, never complete."""
    if len(urls) <= room:
        return urls
    log.warning("%s: over %d sitemap URLs, %d not read", host, MAX_SITEMAP_URLS, len(urls) - room)
    if unread is not None:
        unread.append(f"{len(urls) - room} {what} past the cap of {MAX_SITEMAP_URLS} URLs")
    return urls[:room]


def _get(client: PoliteClient, url: str):
    try:
        return client.get(url)  # Blocked (robots) propagates: it is a FetchError
    except httpx.HTTPStatusError as e:  # a 429/430/5xx the client's retries didn't get past
        raise FetchError(f"HTTP {e.response.status_code} {url}", status=e.response.status_code) from e
    except httpx.HTTPError as e:
        raise FetchError(wrapped_message(e, url)) from e


def _refusal(e: BaseException | None, r: httpx.Response | None = None) -> bool:
    """A pause (429, 430, 503) or an edge's challenge: the store's answer to us, not to this one sitemap, so the
    fetch stops here (ingest.run defers the store, or counts the refusal) instead of reading on without it."""
    if r is not None:
        return r.status_code in BLOCK_STATUSES or is_challenge(r)
    return e is not None and bool(block_status(e) or challenged(e))


def sitemap_urls(client: PoliteClient, host: str, unread: list[str] | None = None) -> list[str]:
    """Every URL in sitemap.xml and, for a sitemap index, in every sitemap it lists. A 404 means the site has none
    ([]); a robots block, network error or any other status of sitemap.xml itself raises FetchError, so a fetch can't
    mistake "couldn't read it" for "nothing there"; so does a sitemap over SITEMAP_MAX_BYTES (ResponseTooLarge).

    One sub-sitemap that can't be read never fails the rest: one robots.txt disallows is skipped; any other failure
    (a 404 or 410, a 5xx, a timeout, a file over the cap) is skipped and its URL added to `unread`, so the caller knows
    the list is incomplete (generic stores the catalog as partial). A 404 or 410 too: the index still lists it, and
    nothing says it held no products, so its products are kept listed (partial), never read as delisted. A pause or a
    challenge (_refusal) still raises: the store is deferred or refused, as for any page."""
    client = client.with_max_response(max(client.max_response_bytes, SITEMAP_MAX_BYTES))
    r = _get(client, f"https://{host}/sitemap.xml")
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise FetchError(f"sitemap HTTP {r.status_code}", status=r.status_code, challenge=is_challenge(r))
    if b"<sitemapindex" not in r.content:
        kept, dropped = _site_locs(r.content, host, "sitemap URLs")
        if dropped and not kept:
            raise _all_off_site(host, dropped)
        return _capped(kept, MAX_SITEMAP_URLS, host, unread, "sitemap URLs")
    locs = _locs(r.content)
    off = [u for u in locs if not _on_site(u, host)]
    if off and len(off) == len(locs):
        raise _all_off_site(host, len(off))
    if off:  # a sub-sitemap elsewhere isn't read; nothing says it held no products, so the list is incomplete
        log.warning("%s: %d sub-sitemaps off the store's site, not read", host, len(off))
        if unread is not None:
            unread.append(f"{len(off)} sub-sitemaps off the store's site")
    locs = [u for u in locs if _on_site(u, host)]
    if len(locs) > MAX_SUB_SITEMAPS:
        log.warning("%s: %d sub-sitemaps, only the first %d read", host, len(locs), MAX_SUB_SITEMAPS)
    urls: list[str] = []
    dropped_urls = 0
    # Product sitemaps first: Shopify/Woo/BigCommerce name them that way.
    ordered = sorted(locs, key=lambda u: "product" not in u.lower())
    if len(ordered) > MAX_SUB_SITEMAPS and unread is not None:
        unread.append(f"{len(ordered) - MAX_SUB_SITEMAPS} sub-sitemaps past the cap of {MAX_SUB_SITEMAPS}")
    for n, sm in enumerate(ordered[:MAX_SUB_SITEMAPS]):
        if len(urls) >= MAX_SITEMAP_URLS:
            log.warning("%s: over %d sitemap URLs, the rest not read", host, MAX_SITEMAP_URLS)
            if unread is not None:
                unread.append(
                    f"{min(len(ordered), MAX_SUB_SITEMAPS) - n} sub-sitemaps past {MAX_SITEMAP_URLS} URLs"
                )
            break
        try:
            sub = _get(client, sm)
        except DeadlineReached:
            raise
        except Blocked:
            log.info("%s: sub-sitemap disallowed by robots.txt, skipped: %s", host, sm)
            continue
        except FetchError as e:
            if _refusal(e):
                raise
            log.warning("%s: sub-sitemap unreadable, skipped: %s", host, e)
            if unread is not None:
                unread.append(f"{sm} (HTTP {e.status})" if e.status else f"{sm} ({e})")
            continue
        if sub.status_code == 200 and not is_challenge(sub):
            kept, dropped = _site_locs(sub.content, host, "sitemap URLs")
            dropped_urls += dropped
            urls.extend(_capped(kept, MAX_SITEMAP_URLS - len(urls), host, unread, f"URLs of {sm}"))
        elif _refusal(None, sub):
            raise FetchError(
                f"sub-sitemap HTTP {sub.status_code}: {sm}",
                status=sub.status_code,
                challenge=is_challenge(sub),
            )
        else:
            log.warning("%s: sub-sitemap HTTP %d, skipped: %s", host, sub.status_code, sm)
            if unread is not None:
                unread.append(f"{sm} (HTTP {sub.status_code})")
    if dropped_urls and not urls:
        raise _all_off_site(host, dropped_urls)
    return urls
