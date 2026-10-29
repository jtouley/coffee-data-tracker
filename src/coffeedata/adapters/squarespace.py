"""Squarespace commerce collections return their items as JSON when you append ?format=json."""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from ..http import FetchError, NotCatalog, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..sitemap import sitemap_urls
from ..text import html_to_text, to_float

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
        if isinstance(data, dict) and "items" not in data and not page:
            raise NotCatalog(f"not a Squarespace collection at {path}")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise FetchError(f"unexpected Squarespace payload at {path}")
        items.extend(
            i
            for i in data.get("items") or []
            if isinstance(i, dict) and ("variants" in i or "productType" in i)
        )
        pagination = data.get("pagination")
        if pagination is None:
            pagination = {}
        if not isinstance(pagination, dict):
            raise FetchError(f"unexpected Squarespace pagination at {path}")
        if not pagination.get("nextPage"):
            break
        offset = pagination.get("nextPageOffset")
        if not isinstance(offset, str | int) or isinstance(offset, bool) or not offset:
            raise FetchError(f"collection {path} has a next page without an offset")
    else:
        raise FetchError(f"collection {path}: still more products after {MAX_PAGES} pages")
    return items


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    """Try likely shop paths until one is a collection with products.

    Returns [] only when every path is missing (404) or a real but empty collection. If no path yields a
    collection and any path was blocked or answered with a non-JSON page, we can't tell "no shop" from
    "couldn't read it", so the fetch fails instead of recording an empty catalog."""
    try:
        urls = sitemap_urls(client, roaster.host)
    except FetchError:
        urls = []  # the common shop paths below are tried anyway, and fail loudly if they can't be read
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
    return list(seen.values())
