"""Read a site's sitemap.xml (following one level of sitemap index)."""

from __future__ import annotations

import re
from itertools import islice

import httpx

from .http import FetchError, PoliteClient

# Matched on the body's bytes, lazily: a sitemap can be tens of MB, and decoding it whole and listing every <loc> (the
# old findall over r.text) held several copies of it per worker. Only the first `limit` URLs are ever kept.
_LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _locs(body: bytes, n: int | None = None) -> list[str]:
    return [m.group(1).decode("utf-8", "replace") for m in islice(_LOC.finditer(body), n)]


def _get(client: PoliteClient, url: str):
    try:
        return client.get(url)  # Blocked (robots) propagates: it is a FetchError
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__} {url}") from e


def sitemap_urls(client: PoliteClient, host: str, limit: int = 5000) -> list[str]:
    """URLs from sitemap.xml. A 404 means the site has none ([]); a robots block, network error or any other
    status raises FetchError, so a fetch can't mistake "couldn't read it" for "nothing there"."""
    r = _get(client, f"https://{host}/sitemap.xml")
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise FetchError(f"sitemap HTTP {r.status_code}")
    if b"<sitemapindex" not in r.content:
        return _locs(r.content, limit)
    locs = _locs(r.content)
    urls: list[str] = []
    # Product sitemaps first: Shopify/Woo/BigCommerce name them that way.
    for sm in sorted(locs, key=lambda u: "product" not in u.lower())[:5]:
        sub = _get(client, sm)
        if sub.status_code != 200:
            raise FetchError(f"sub-sitemap HTTP {sub.status_code}: {sm}")
        urls.extend(_locs(sub.content, limit - len(urls)))
        if len(urls) >= limit:
            break
    return urls[:limit]
