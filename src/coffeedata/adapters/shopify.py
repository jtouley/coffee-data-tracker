"""Shopify storefronts publish their catalog at /products.json."""

from __future__ import annotations

from ..http import PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams
from ..text import html_to_text, to_float

MAX_PAGES = 20


def parse_product(p: dict, roaster: Roaster) -> CatalogItem:
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    variants = []
    for v in p.get("variants") or []:
        title = " ".join(str(v.get(k) or "") for k in ("title", "option1", "option2", "option3"))
        grams = parse_grams(title)
        if grams is None and 30 <= (v.get("grams") or 0) <= 25_000:
            grams = int(v["grams"])
        variants.append(Variant(variant_id=str(v["id"]), title=str(v.get("title") or ""),
                                price_usd=to_float(v.get("price")), grams=grams,
                                available=v.get("available")))
    images = p.get("images") or []
    return CatalogItem(
        roaster_slug=roaster.slug, product_id=str(p["id"]), handle=p.get("handle") or str(p["id"]),
        url=f"https://{roaster.host}/products/{p.get('handle')}", title=(p.get("title") or "").strip(),
        platform="shopify", vendor=p.get("vendor") or "", product_type=p.get("product_type") or "",
        tags=sorted(tags), image=images[0].get("src") if images else None, variants=variants,
        body_text=html_to_text(p.get("body_html")),
    )


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    items: list[CatalogItem] = []
    for page in range(1, MAX_PAGES + 1):
        data = client.get_json(f"https://{roaster.host}/products.json", limit=250, page=page)
        products = data.get("products") if isinstance(data, dict) else None
        if not products:
            break
        items.extend(parse_product(p, roaster) for p in products)
        if len(products) < 250:
            break
    return items
