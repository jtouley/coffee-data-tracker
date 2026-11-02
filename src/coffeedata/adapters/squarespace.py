"""Squarespace commerce collections return their items as JSON when you append ?format=json."""

from __future__ import annotations

import logging
from collections import Counter
from urllib.parse import urlparse

from ..http import FetchError, NotCatalog, PoliteClient, block_status
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..ratelimit import RateLimited
from ..sitemap import sitemap_urls
from ..text import html_to_text, to_float

log = logging.getLogger(__name__)

COMMON_SHOP_PATHS = ["/shop", "/store", "/coffee", "/shop-coffee", "/coffees", "/beans"]
MAX_PAGES = 10


def shop_paths(urls: list[str]) -> list[str]:
    """Collection paths ranked by how many sitemap URLs sit beneath them, then common names."""
    parents: Counter[str] = Counter()
    for u in urls:
        parts = [p for p in urlparse(u).path.split("/") if p]
        if len(parts) >= 2:
            parents["/" + parts[0]] += 1
    ranked = [p for p, n in parents.most_common(4) if n >= 2]
    return list(dict.fromkeys(ranked + COMMON_SHOP_PATHS))


def parse_item(i: dict, roaster: Roaster) -> CatalogItem:
    variants = []
    for v in i.get("variants") or []:
        title = " ".join(str(x) for x in (v.get("attributes") or {}).values())
        stock = v.get("unlimited") or (v.get("qtyInStock") or 0) > 0
        variants.append(
            Variant(
                variant_id=str(v.get("id") or v.get("sku")),
                title=title,
                price_usd=to_float((v.get("priceMoney") or {}).get("value")),
                grams=parse_grams(title),
                available=bool(stock),
            )
        )
    return CatalogItem(
        roaster_id=roaster.roaster_id,
        product_id=str(i["id"]),
        handle=i.get("urlId") or str(i["id"]),
        url=f"https://{roaster.host}{i.get('fullUrl', '')}",
        title=(i.get("title") or "").strip(),
        platform="squarespace",
        product_type=",".join(i.get("categories") or []),
        tags=sorted(set(i.get("tags") or []) | set(i.get("categories") or [])),
        image=i.get("assetUrl"),
        variants=variants,
        body_text=html_to_text(i.get("body") or i.get("excerpt")),
        payload_hash=payload_hash(i),
    )


def _collection(client: PoliteClient, roaster: Roaster, path: str) -> list[dict] | None:
    """Product items in a collection; None when the path doesn't exist (404). A first page that isn't JSON
    raises NotCatalog; any other failure, on any page, raises FetchError."""
    items: list[dict] = []
    offset: str | int | None = None
    for page in range(MAX_PAGES):
        params: dict[str, str | int | float | bool | None] = {"format": "json"}
        if offset:
            params["offset"] = offset
        try:
            data = client.catalog_json(f"https://{roaster.host}{path}", **params)
        except NotCatalog:
            if page:
                raise FetchError(f"collection {path} page {page + 1} is not JSON") from None
            raise
        if data is None:
            if page:  # a later page vanishing would silently truncate the catalog
                raise FetchError(f"collection {path} page {page + 1} returned 404")
            return None
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise FetchError(f"unexpected Squarespace payload at {path}")
        items.extend(
            i
            for i in data.get("items") or []
            if isinstance(i, dict) and ("variants" in i or "productType" in i)
        )
        pagination = data.get("pagination") or {}
        offset = pagination.get("nextPageOffset") if pagination.get("nextPage") else None
        if not offset:
            break
    return items


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    """Try likely shop paths until one is a collection with products.

    Returns [] only when every path is missing (404) or a real but empty collection. If no path yields a
    collection and any path was blocked or answered with a non-JSON page, we can't tell "no shop" from
    "couldn't read it", so the fetch fails instead of recording an empty catalog."""
    sitemap_block: FetchError | None = None
    try:
        urls = sitemap_urls(client, roaster.host)
    except FetchError as e:
        if block_status(e) or isinstance(
            e, RateLimited
        ):  # the sitemap's block, or the limiter's pause for it
            if (
                not client.is_last_try
            ):  # a pause: the store is deferred (ingest.run) and its sitemap asked again
                raise
            # Its deferred retries are spent and the sitemap still blocks: the common shop paths, so a store whose
            # sitemap always answers 429 or 503 still has its catalog read each week.
            sitemap_block = e
        # the common shop paths below are tried anyway, and fail loudly if they can't be read; the sitemap's own
        # shop paths are lost for this fetch, so say why (a sitemap over sitemap.SITEMAP_MAX_BYTES, a block, a 5xx)
        log.warning("%s: sitemap unreadable, trying the common shop paths only: %s", roaster.roaster_id, e)
        urls = []
    seen: dict[str, CatalogItem] = {}
    found_collection = False
    unreadable: list[str] = []
    for path in shop_paths(urls)[:6]:
        try:
            raw_items = _collection(client, roaster, path)
        except NotCatalog as e:
            unreadable.append(str(e))
            continue
        if raw_items is None:
            continue
        found_collection = True
        for raw in raw_items:
            if raw.get("id"):
                item = parse_item(raw, roaster)
                seen.setdefault(item.product_id, item)
        if seen:
            break
    if not found_collection and unreadable:
        raise FetchError(f"no readable shop collection ({len(unreadable)} non-JSON pages)")
    if sitemap_block is not None and not seen:
        # without the sitemap's own shop paths, no products is no answer: never recorded as an empty catalog
        raise FetchError(f"no products on the common shop paths; {sitemap_block}") from sitemap_block
    return list(seen.values())
