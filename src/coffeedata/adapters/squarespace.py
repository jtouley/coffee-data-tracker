"""Squarespace commerce collections return their items as JSON when you append ?format=json."""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from ..http import Blocked, FetchError, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams
from ..sitemap import sitemap_urls
from ..text import html_to_text, to_float

COMMON_SHOP_PATHS = ["/shop", "/store", "/coffee", "/shop-coffee", "/coffees", "/beans"]
MAX_PAGES = 10


def shop_paths(urls: list[str]) -> list[str]:
    """Collection paths ranked by how many sitemap URLs sit beneath them, then common names."""
    parents = Counter()
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
        variants.append(Variant(variant_id=str(v.get("id") or v.get("sku")), title=title,
                                price_usd=to_float((v.get("priceMoney") or {}).get("value")),
                                grams=parse_grams(title), available=bool(stock)))
    return CatalogItem(
        roaster_id=roaster.roaster_id, product_id=str(i["id"]), handle=i.get("urlId") or str(i["id"]),
        url=f"https://{roaster.host}{i.get('fullUrl', '')}", title=(i.get("title") or "").strip(),
        platform="squarespace", product_type=",".join(i.get("categories") or []),
        tags=sorted(set(i.get("tags") or []) | set(i.get("categories") or [])),
        image=i.get("assetUrl"), variants=variants,
        body_text=html_to_text(i.get("body") or i.get("excerpt")),
    )


def _collection(client: PoliteClient, roaster: Roaster, path: str) -> list[dict] | None:
    """Products in a collection; None when the path isn't a collection (404, not JSON)."""
    items: list[dict] = []
    offset: object = None
    for page in range(MAX_PAGES):
        params: dict[str, object] = {"format": "json"}
        if offset:
            params["offset"] = offset
        try:
            data = client.get_json(f"https://{roaster.host}{path}", **params)
        except Blocked:
            return None
        if not isinstance(data, dict):
            if page:  # a later page failing would silently truncate the catalog
                raise FetchError(f"collection {path} page {page + 1} failed")
            return None
        items.extend(i for i in data.get("items") or [] if "variants" in i or "productType" in i)
        page = data.get("pagination") or {}
        offset = page.get("nextPageOffset") if page.get("nextPage") else None
        if not offset:
            break
    return items


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    seen: dict[str, CatalogItem] = {}
    for path in shop_paths(sitemap_urls(client, roaster.host))[:6]:
        for raw in _collection(client, roaster, path) or []:
            if isinstance(raw, dict) and raw.get("id"):
                item = parse_item(raw, roaster)
                seen.setdefault(item.product_id, item)
        if seen:
            break
    return list(seen.values())
