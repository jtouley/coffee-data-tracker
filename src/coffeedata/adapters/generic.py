"""Fallback for any platform: product pages from sitemap.xml, parsed via schema.org Product JSON-LD."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

import httpx

from ..http import (
    Blocked,
    DeadlineReached,
    FetchError,
    PartialCatalog,
    PoliteClient,
    block_status,
    challenge_error,
)
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..sitemap import sitemap_urls
from ..text import html_to_text, scrub_surrogates, to_float

# Shopify/Woo /product(s)/x, Wix /product-page/x, Square Online /product/x/ID, BigCommerce /x/ under /shop/.
_PRODUCTISH = re.compile(
    r"/(product-page|products?|shop|store|coffees?|beans|items?|p)/[^/]+(/[A-Za-z0-9]{6,})?/?$", re.I
)
_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


_BEANY = re.compile(
    r"coffee|roast|blend|espresso|decaf|origin|bean|natural|washed|honey|"
    r"ethiopia|colombia|kenya|guatemala|brazil|peru|honduras|mexico|sumatra|rwanda|burundi",
    re.I,
)
_MERCHY = re.compile(
    r"mug|shirt|tee|hat|cap|sticker|tumbler|hoodie|gift|card|grinder|filter|subscription", re.I
)


def product_urls(urls: list[str]) -> list[str]:
    """Every product-looking URL, bean-looking slugs first and merch-looking slugs last. No cap: the old MAX_PAGES = 80
    read a different 80 of a larger catalog each week, unmarked (dq-audit 2026-11-02)."""
    prods = [u for u in urls if _PRODUCTISH.search(urlparse(u).path)]
    rank = lambda u: (bool(_MERCHY.search(u)), not _BEANY.search(urlparse(u).path))  # noqa: E731
    return sorted(prods, key=rank)


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
            out.extend(_walk(scrub_surrogates(json.loads(block.strip()))))
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
        if (
            vid in seen
        ):  # some stores repeat one SKU across every offer; variant ids must be unique per product
            vid = f"{vid}#{i}"
        seen.add(vid)
        variants.append(
            Variant(
                variant_id=vid,
                title=title,
                price_usd=to_float(o.get("price") or o.get("lowPrice")),
                grams=parse_grams(title) or parse_grams(str(p.get("name"))),
                available=("InStock" in avail) if avail else None,
            )
        )
    image = p.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    handle = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    pid = str(p.get("productID") or p.get("sku") or hashlib.sha1(url.encode()).hexdigest()[:12])
    return CatalogItem(
        roaster_id=roaster.roaster_id,
        product_id=pid,
        handle=handle,
        url=url,
        title=html_to_text(str(p.get("name") or handle)),
        platform="generic",
        product_type=str(p.get("category") or ""),
        image=image,
        variants=variants,
        body_text=html_to_text(str(p.get("description") or "")),
        payload_hash=payload_hash(p),
    )


# The most failed product page URLs a partial fetch's reason names; it always counts them all.
MAX_FAILED_URLS_NAMED = 5


def _partial_reason(failed: list[str], unread: list[str]) -> str:
    parts = []
    if failed:
        named = "; ".join(failed[:MAX_FAILED_URLS_NAMED])
        more = (
            f"; and {len(failed) - MAX_FAILED_URLS_NAMED} more" if len(failed) > MAX_FAILED_URLS_NAMED else ""
        )
        parts.append(f"{len(failed)} product pages failed: {named}{more}")
    if unread:
        parts.append(f"{len(unread)} sub-sitemaps unread: {'; '.join(unread)}")
    return "; ".join(parts)


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    items: dict[str, CatalogItem] = {}
    failed: list[str] = []  # product pages that couldn't be read: "url (why)"
    # The sitemap is the only list of product pages: one it can't read (a block, a 5xx, or a file over the protocol's
    # 50 MB, sitemap.SITEMAP_MAX_BYTES) fails the fetch, never an empty catalog. A sub-sitemap it couldn't read
    # (`unread`) leaves the list incomplete: the products read are stored as partial, never as the whole catalog.
    unread: list[str] = []
    urls = product_urls(sitemap_urls(client, roaster.host, unread))
    # Every product page is read: no cap, and no giving up after the first pages without Product markup (the old
    # GIVE_UP_AFTER = 10 stored the rest of the catalog as empty). Only the run's deadline stops it: partial.
    for n, url in enumerate(urls):
        try:
            r = client.get(url)
        except DeadlineReached as e:
            why = "; ".join(filter(None, [str(e), _partial_reason(failed, unread)]))
            raise PartialCatalog(
                f"{why} ({n} of {len(urls)} product pages read)", list(items.values())
            ) from e
        except (Blocked, httpx.HTTPError) as e:
            if status := block_status(e):  # a pause: stop asking now, and the store is deferred (ingest.run)
                raise FetchError(f"HTTP {status} {url}", status=status) from e
            why = (
                f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
            )
            failed.append(f"{url} ({why})")
            continue
        if err := challenge_error(r, url):
            # an edge's challenge (a 202, or a challenge header): a refusal, like a block, never a failed page
            raise err
        if r.status_code in (404, 410):
            continue  # a sitemap entry for a product that's gone: it's delisted, not unreadable
        if r.status_code != 200:
            failed.append(f"{url} (HTTP {r.status_code})")
            continue
        for p in jsonld_products(r.text)[:1]:  # the page's own product; later blocks are related products
            item = parse_jsonld(p, url, roaster)
            items.setdefault(item.product_id, item)
    if failed or unread:
        # A product page or a sub-sitemap that couldn't be read: the products on it would look delisted, so the
        # products read are stored as partial (kept listed, never delisted), and the rest of the catalog isn't lost.
        # Nothing read at all is the fetch's error, never an empty catalog.
        msg = _partial_reason(failed, unread)
        if not items:
            raise FetchError(msg)
        raise PartialCatalog(msg, list(items.values()))
    return list(items.values())
