"""Read a site's sitemap.xml (following one level of sitemap index)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

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


_URL = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_SITEMAP = re.compile(r"<sitemap>(.*?)</sitemap>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)", re.I)
_IMAGE_TITLE = re.compile(r"<image:title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</image:title>", re.S | re.I)


@dataclass
class Entry:
    url: str
    lastmod: str = ""
    title: str = ""  # the product's title, where the sitemap carries one (Shopify's image:title)


def product_sitemaps(xml: str) -> list[str]:
    """The product sitemaps a sitemap index lists (Shopify's sitemap_products_N, Yoast's product-sitemap)."""
    locs = [unescape(m.group(1)) for b in _SITEMAP.findall(xml) if (m := _LOC.search(b))]
    return [u for u in locs if "product" in u.lower()]


def entries(xml: str) -> list[Entry]:
    """Each <url> of a urlset, with its lastmod and image title, in order and once each."""
    out: dict[str, Entry] = {}
    for block in _URL.findall(xml):
        if not (loc := _LOC.search(block)):
            continue
        url = unescape(loc.group(1))
        lastmod, title = _LASTMOD.search(block), _IMAGE_TITLE.search(block)
        out.setdefault(
            url,
            Entry(
                url, lastmod.group(1) if lastmod else "", unescape(title.group(1)).strip() if title else ""
            ),
        )
    return list(out.values())
