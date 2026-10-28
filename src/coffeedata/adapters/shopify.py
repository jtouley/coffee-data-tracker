"""Shopify storefronts publish their catalog at /products.json."""

from __future__ import annotations

from datetime import UTC, datetime

from ..http import FetchError, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams
from ..text import html_to_text, to_float

MAX_PAGES = 20


def _published_at(value: object) -> datetime | None:
    """One odd date must never cost a roaster its catalog: anything unreadable is None. No offset means UTC."""
    try:
        t = datetime.fromisoformat(str(value)) if value else None
        if t is None:
            return None
        return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError):
        return None


def parse_product(p: dict, roaster: Roaster) -> CatalogItem:
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    variants = []
    for v in p.get("variants") or []:
        title = " ".join(str(v.get(k) or "") for k in ("title", "option1", "option2", "option3"))
        grams = parse_grams(title)
        if grams is None and isinstance(v.get("grams"), int) and 30 <= v["grams"] <= 25_000:
            grams = v["grams"]
        variants.append(
            Variant(
                variant_id=str(v["id"]),
                title=str(v.get("title") or ""),
                price_usd=to_float(v.get("price")),
                grams=grams,
                available=v.get("available"),
            )
        )
    image = next((i.get("src") if isinstance(i, dict) else str(i) for i in p.get("images") or []), None)
    return CatalogItem(
        roaster_id=roaster.roaster_id,
        product_id=str(p["id"]),
        handle=p.get("handle") or str(p["id"]),
        url=f"https://{roaster.host}/products/{p.get('handle')}",
        title=(p.get("title") or "").strip(),
        platform="shopify",
        vendor=p.get("vendor") or "",
        product_type=p.get("product_type") or "",
        tags=sorted(map(str, tags)),
        image=image,
        variants=variants,
        body_text=html_to_text(p.get("body_html")),
        published_at=_published_at(p.get("published_at")),
    )


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    items: dict[str, CatalogItem] = {}
    for page in range(1, MAX_PAGES + 1):
        data = client.fetch_json(f"https://{roaster.host}/products.json", limit=250, page=page)
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise FetchError(f"unexpected /products.json payload from {roaster.host}")
        # A full page can hold fewer than 250 products (Shopify drops some after paging; onyxcoffeelab.com's pages
        # hold 249, 247, 234, 242), so only an empty page ends the catalog. A store that ignores `page` and
        # repeats itself ends it too.
        new = {i.product_id: i for i in (parse_product(p, roaster) for p in data["products"])}
        if not new.keys() - items.keys():
            break
        items |= new
    return list(items.values())
