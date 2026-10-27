"""Fallback for any platform: product pages from sitemap.xml, parsed via schema.org Product JSON-LD."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

import httpx

from ..http import Blocked, FetchError, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams
from ..sitemap import sitemap_urls
from ..text import html_to_text, to_float

MAX_PAGES = 80
# Shopify/Woo /product(s)/x, Wix /product-page/x, Square Online /product/x/ID, BigCommerce /x/ under /shop/.
_PRODUCTISH = re.compile(r"/(product-page|products?|shop|store|coffees?|beans|items?|p)/[^/]+(/[A-Za-z0-9]{6,})?/?$", re.I)
_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


_BEANY = re.compile(r"coffee|roast|blend|espresso|decaf|origin|bean|natural|washed|honey|"
                    r"ethiopia|colombia|kenya|guatemala|brazil|peru|honduras|mexico|sumatra|rwanda|burundi", re.I)
_MERCHY = re.compile(r"mug|shirt|tee|hat|cap|sticker|tumbler|hoodie|gift|card|grinder|filter|subscription", re.I)


def product_urls(urls: list[str]) -> list[str]:
    """Product-looking URLs, bean-looking slugs first and merch-looking slugs last, capped at MAX_PAGES."""
    prods = [u for u in urls if _PRODUCTISH.search(urlparse(u).path)]
    rank = lambda u: (bool(_MERCHY.search(u)), not _BEANY.search(urlparse(u).path))  # noqa: E731
    return sorted(prods, key=rank)[:MAX_PAGES]


def _walk(node: object):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types:
            yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk(node[key])


def jsonld_products(html: str) -> list[dict]:
    out = []
    for block in _LD.findall(html):
        try:
            out.extend(_walk(json.loads(block.strip())))
        except ValueError:
            continue
    return out


def parse_jsonld(p: dict, url: str, roaster: Roaster) -> CatalogItem:
    offers = p.get("offers") or []
    if isinstance(offers, dict):
        offers = offers.get("offers") or [offers]
    variants, seen = [], set()
    for i, o in enumerate(o for o in offers if isinstance(o, dict)):
        title = str(o.get("name") or o.get("sku") or "")
        avail = str(o.get("availability") or "")
        vid = str(o.get("sku") or i)
        if vid in seen:  # some stores repeat one SKU across every offer; variant ids must be unique per product
            vid = f"{vid}#{i}"
        seen.add(vid)
        variants.append(Variant(variant_id=vid, title=title,
                                price_usd=to_float(o.get("price") or o.get("lowPrice")),
                                grams=parse_grams(title) or parse_grams(str(p.get("name"))),
                                available=("InStock" in avail) if avail else None))
    image = p.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    handle = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    pid = str(p.get("productID") or p.get("sku") or hashlib.sha1(url.encode()).hexdigest()[:12])
    return CatalogItem(
        roaster_id=roaster.roaster_id, product_id=pid, handle=handle, url=url,
        title=html_to_text(str(p.get("name") or handle)), platform="generic",
        product_type=str(p.get("category") or ""), image=image, variants=variants,
        body_text=html_to_text(str(p.get("description") or "")),
    )


GIVE_UP_AFTER = 10  # pages without Product markup before assuming the site has none


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    items: dict[str, CatalogItem] = {}
    failed = 0
    urls = product_urls(sitemap_urls(client, roaster.host))
    for n, url in enumerate(urls):
        if n >= GIVE_UP_AFTER and not items:
            break
        try:
            r = client.get(url)
        except (Blocked, httpx.HTTPError):
            failed += 1
            continue
        if r.status_code in (404, 410):
            continue  # a sitemap entry for a product that's gone: it's delisted, not unreadable
        if r.status_code != 200:
            failed += 1
            continue
        for p in jsonld_products(r.text)[:1]:
            item = parse_jsonld(p, url, roaster)
            items.setdefault(item.product_id, item)
    if failed:
        # Any unreadable product page makes this a partial catalog, and the missing products would look
        # delisted. Fail the fetch; it is retried and never stored as a successful catalog.
        raise FetchError(f"{failed} of {min(len(urls), MAX_PAGES)} product pages failed")
    return list(items.values())
