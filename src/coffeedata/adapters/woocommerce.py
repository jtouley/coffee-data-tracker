"""WooCommerce stores expose the public Store API at /wp-json/wc/store/v1/products."""

from __future__ import annotations

from ..http import PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams
from ..text import html_to_text

MAX_PAGES = 20


def _price(prices: dict) -> float | None:
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    return int(raw) / 10 ** int(prices.get("currency_minor_unit", 2))


def parse_product(p: dict, roaster: Roaster) -> CatalogItem:
    tags = sorted({t["name"] for t in p.get("tags", [])} | {c["name"] for c in p.get("categories", [])})
    weights = [a for a in p.get("attributes", []) if "weight" in a.get("name", "").lower()
               or "size" in a.get("name", "").lower()]
    sizes = [t["name"] for a in weights for t in a.get("terms", [])] or [p.get("name", "")]
    price = _price(p.get("prices") or {})
    variants = [Variant(variant_id=f"{p['id']}:{i}", title=s, price_usd=price, grams=parse_grams(s),
                        available=p.get("is_in_stock")) for i, s in enumerate(sizes)]
    images = p.get("images") or []
    body = "\n".join(filter(None, [html_to_text(p.get("short_description")), html_to_text(p.get("description"))]))
    return CatalogItem(
        roaster_slug=roaster.slug, product_id=str(p["id"]), handle=p.get("slug") or str(p["id"]),
        url=p.get("permalink") or f"https://{roaster.host}/?p={p['id']}", title=html_to_text(p.get("name")),
        platform="woocommerce", product_type=p.get("type") or "", tags=tags,
        image=images[0].get("src") if images else None, variants=variants, body_text=body,
    )


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    items: list[CatalogItem] = []
    for page in range(1, MAX_PAGES + 1):
        data = client.get_json(f"https://{roaster.host}/wp-json/wc/store/v1/products", per_page=100, page=page)
        if not isinstance(data, list) or not data:
            break
        items.extend(parse_product(p, roaster) for p in data)
        if len(data) < 100:
            break
    return items
