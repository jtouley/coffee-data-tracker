"""Read a site's sitemap.xml (following one level of sitemap index)."""

from __future__ import annotations

import logging
import re

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
)

log = logging.getLogger(__name__)

# Matched on the body's bytes, lazily: a sitemap can be tens of MB, and decoding it whole (the old findall over r.text)
# held several copies of it per worker. Every URL is kept: no count limit (the old 5,000 URLs and 5 sub-sitemaps cut
# large catalogs), only the per-response byte cap below.
_LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)
# The sitemap protocol allows 50 MB (52,428,800 bytes) uncompressed per file, above the client's usual response cap
# (http.MAX_RESPONSE_BYTES); sitemap requests get this one instead. A larger file is out of spec: ResponseTooLarge.
SITEMAP_MAX_BYTES = 64 * 2**20


def _locs(body: bytes) -> list[str]:
    return [m.group(1).decode("utf-8", "replace") for m in _LOC.finditer(body)]


def _get(client: PoliteClient, url: str):
    try:
        return client.get(url)  # Blocked (robots) propagates: it is a FetchError
    except httpx.HTTPStatusError as e:  # a 429/430/5xx the client's retries didn't get past
        raise FetchError(f"HTTP {e.response.status_code} {url}", status=e.response.status_code) from e
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__} {url}") from e


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
        return _locs(r.content)
    locs = _locs(r.content)
    urls: list[str] = []
    # Product sitemaps first: Shopify/Woo/BigCommerce name them that way.
    for sm in sorted(locs, key=lambda u: "product" not in u.lower()):
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
            urls.extend(_locs(sub.content))
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
    return urls
