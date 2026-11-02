"""Squarespace commerce collections return their items as JSON when you append ?format=json."""

from __future__ import annotations

import logging
from collections import Counter
from itertools import count
from urllib.parse import urlparse

from ..http import DeadlineReached, FetchError, NotCatalog, PartialCatalog, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..sitemap import sitemap_urls
from ..text import html_to_text, to_float

log = logging.getLogger(__name__)

COMMON_SHOP_PATHS = ["/shop", "/store", "/coffee", "/shop-coffee", "/coffees", "/beans"]
# No cap on a collection's pages (it was 10), or on the shop paths tried (6, from the 4 busiest sitemap parents):
# pages are read until the store says there is no next one. Only the run's deadline stops early: partial.


def shop_paths(urls: list[str]) -> list[str]:
    """Every collection path with 2 or more sitemap URLs beneath it, busiest first, then the common names."""
    parents: Counter[str] = Counter()
    for u in urls:
        parts = [p for p in urlparse(u).path.split("/") if p]
        if len(parts) >= 2:
            parents["/" + parts[0]] += 1
    ranked = [p for p, n in parents.most_common() if n >= 2]
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
    raises NotCatalog; any other failure, on any page, raises FetchError. At the run's deadline, PartialCatalog with
    the items read so far (raw dicts)."""
    items: list[dict] = []
    offset: str | int | None = None
    offsets: set[str] = set()  # a store that hands back an offset it already gave has nothing new: the end
    for page in count():
        params: dict[str, str | int | float | bool | None] = {"format": "json"}
        if offset:
            params["offset"] = offset
        try:
            data = client.catalog_json(f"https://{roaster.host}{path}", **params)
        except DeadlineReached as e:
            raise PartialCatalog(f"{e} ({path} page {page + 1})", items) from e
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
        if not offset or str(offset) in offsets:
            break
        offsets.add(str(offset))
    return items


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    """Try likely shop paths until one is a collection with products.

    Returns [] only when every path is missing (404) or a real but empty collection. If no path yields a
    collection and any path was blocked or answered with a non-JSON page, we can't tell "no shop" from
    "couldn't read it", so the fetch fails instead of recording an empty catalog."""
    try:
        urls = sitemap_urls(client, roaster.host)
    except DeadlineReached as e:
        raise PartialCatalog(str(e), []) from e
    except FetchError as e:
        # the common shop paths below are tried anyway, and fail loudly if they can't be read; the sitemap's own
        # shop paths are lost for this fetch, so say why (a sitemap over sitemap.SITEMAP_MAX_BYTES, a block, a 5xx)
        log.warning("%s: sitemap unreadable, trying the common shop paths only: %s", roaster.roaster_id, e)
        urls = []
    seen: dict[str, CatalogItem] = {}
    found_collection = False
    unreadable: list[str] = []
    for path in shop_paths(urls):
        try:
            raw_items = _collection(client, roaster, path)
        except PartialCatalog as e:
            for raw in e.items:
                if raw.get("id"):
                    seen.setdefault(str(raw["id"]), parse_item(raw, roaster))
            raise PartialCatalog(str(e), list(seen.values())) from e
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
    return list(seen.values())
