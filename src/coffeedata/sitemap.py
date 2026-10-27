"""Read a site's sitemap.xml (following one level of sitemap index)."""

from __future__ import annotations

import re

import httpx

from .http import FetchError, PoliteClient

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


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
    locs = _LOC.findall(r.text)
    if "<sitemapindex" not in r.text:
        return locs[:limit]
    urls: list[str] = []
    # Product sitemaps first: Shopify/Woo/BigCommerce name them that way.
    for sm in sorted(locs, key=lambda u: "product" not in u.lower())[:5]:
        sub = _get(client, sm)
        if sub.status_code != 200:
            raise FetchError(f"sub-sitemap HTTP {sub.status_code}: {sm}")
        urls.extend(_LOC.findall(sub.text))
        if len(urls) >= limit:
            break
    return urls[:limit]
