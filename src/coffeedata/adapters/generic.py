"""Fallback for any platform: product pages from sitemap.xml, parsed via schema.org Product JSON-LD."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING
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
from ..models import CatalogItem, Roaster, Variant, currency_code, parse_grams, payload_hash
from ..ratelimit import SHOPIFY_HOSTED
from ..sitemap import sitemap_urls
from ..text import html_to_text, scrub_surrogates, to_float

if TYPE_CHECKING:
    from .shopify import Identity

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


def _price_currency(offer: dict, default: str | None = None) -> str | None:
    """An offer's currency as stated: schema.org's `priceCurrency`, "The currency of the price ... Use standard
    formats: ISO 4217 currency format" (https://schema.org/priceCurrency), on the Offer itself, else on its
    `priceSpecification` (a PriceSpecification also takes priceCurrency), else on the AggregateOffer that holds it
    (default)."""
    if (own := currency_code(offer.get("priceCurrency"))) is not None:
        return own
    spec = offer.get("priceSpecification")
    for s in spec if isinstance(spec, list) else [spec]:
        if isinstance(s, dict) and (c := currency_code(s.get("priceCurrency"))) is not None:
            return c
    return default


def parse_jsonld(p: dict, url: str, roaster: Roaster) -> CatalogItem:
    offers = p.get("offers") or []
    outer: str | None = None  # an AggregateOffer's own priceCurrency, for offers inside it that state none
    if isinstance(offers, dict):
        outer = _price_currency(offers)
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
                currency=_price_currency(o, outer),
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


# Shopify's sitemap index names its children sitemap_products_1.xml, sitemap_pages_1.xml... (with ?from=&to= ranges):
# the store is a Shopify storefront. Run 2026-11-16: 23 generic stores newly answered Cloudflare's challenge on the
# dedicated IP, most on Shopify's sitemap files and /products/ pages (one request per product).
_SHOPIFY_SITEMAP = re.compile(r"/sitemap_(products|pages|collections|blogs|articles)_\d+\.xml", re.I)


def shopify_sitemap(index: list[str]) -> bool:
    """A sitemap index that lists Shopify's own product sitemaps (sitemap_products_N.xml)."""
    return any(
        _SHOPIFY_SITEMAP.search(urlparse(u).path) and "/sitemap_products_" in urlparse(u).path.lower()
        for u in index
    )


def _shopify_catalog(
    client: PoliteClient,
    roaster: Roaster,
    resume: dict | None,
    known_products: int | None,
    identity: Identity | None = None,
) -> list[CatalogItem] | None:
    """A Shopify storefront's catalog from /products.json (shopify.fetch: one request per 250 products, instead of one
    per product page), or None when the store doesn't serve it (shopify.not_served). A block, a challenge or a
    deadline is the fetch's answer, as for any Shopify store: never a reason to read the pages instead."""
    from . import shopify

    try:
        items = shopify.fetch(
            client, roaster, resume=resume, known_products=known_products, identity=identity
        )
    except FetchError as e:
        if not shopify.not_served(e):
            raise
        client.note_route(f"pages: /products.json not served ({e})"[:300])
        return None
    client.note_route("shopify /products.json: its sitemap index is Shopify's")
    return items


def fetch(
    client: PoliteClient,
    roaster: Roaster,
    resume: dict | None = None,
    known_products: int | None = None,
    identity: Identity | None = None,
) -> list[CatalogItem]:
    """A store's catalog from its sitemap's product pages (schema.org JSON-LD). A store whose sitemap index turns out
    to be Shopify's (sitemap_products_N.xml), not yet known as one to the registry, is read from /products.json
    instead, and from then on every request it makes counts against the Shopify-hosted budget
    (ratelimit.SHOPIFY_HOSTED). The probe marks such a store `shopify?` (discover.probe_one), and from then on the
    Shopify job crawls it (models.Roaster.adapter). resume, known_products and identity: as shopify.fetch (the
    products this adapter stored keep their ids)."""
    return read_pages(client, roaster, resume, known_products, detect_shopify=True, identity=identity)


def read_pages(
    client: PoliteClient,
    roaster: Roaster,
    resume: dict | None = None,
    known_products: int | None = None,
    detect_shopify: bool = False,
    identity: Identity | None = None,
) -> list[CatalogItem]:
    """The catalog from the sitemap's product pages, each page's JSON-LD (shopify.fetch reads a `shopify?` store's
    pages this way when it has no /products.json)."""
    items: dict[str, CatalogItem] = {}
    failed: list[str] = []  # product pages that couldn't be read: "url (why)"
    # The sitemap is the only list of product pages: one it can't read (a block, a 5xx, or a file over the protocol's
    # 50 MB, sitemap.SITEMAP_MAX_BYTES) fails the fetch, never an empty catalog. A sub-sitemap it couldn't read
    # (`unread`) leaves the list incomplete: the products read are stored as partial, never as the whole catalog.
    unread: list[str] = []
    index: list[str] = []
    found = sitemap_urls(client, roaster.host, unread, index)
    if detect_shopify and shopify_sitemap(index):
        client = client.for_platform(SHOPIFY_HOSTED)  # a Shopify storefront: its requests share that budget
        if (shop := _shopify_catalog(client, roaster, resume, known_products, identity)) is not None:
            return shop
    urls = product_urls(found)
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
