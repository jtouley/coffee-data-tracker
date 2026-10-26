"""Read a site's sitemap.xml (following one level of sitemap index)."""

from __future__ import annotations

import re

from .http import Blocked, PoliteClient

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def sitemap_urls(client: PoliteClient, host: str, limit: int = 5000) -> list[str]:
    try:
        r = client.get(f"https://{host}/sitemap.xml")
    except (Blocked, Exception):
        return []
    if r.status_code != 200:
        return []
    locs = _LOC.findall(r.text)
    if "<sitemapindex" not in r.text:
        return locs[:limit]
    urls: list[str] = []
    # Product sitemaps first: Shopify/Woo/BigCommerce name them that way.
    for sm in sorted(locs, key=lambda u: "product" not in u.lower())[:5]:
        try:
            sub = client.get(sm)
        except (Blocked, Exception):
            continue
        if sub.status_code == 200:
            urls.extend(_LOC.findall(sub.text))
        if len(urls) >= limit:
            break
    return urls[:limit]
