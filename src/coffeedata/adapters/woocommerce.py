"""WooCommerce stores expose the public Store API at /wp-json/wc/store/v1/products."""

from __future__ import annotations

from ..http import FetchError, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..text import html_to_text

MAX_PAGES = 20
MAX_VARIATION_LOOKUPS = 150  # per roaster, per run


def _price(prices: dict | None) -> float | None:
    prices = prices or {}
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    return int(raw) / 10 ** int(prices.get("currency_minor_unit", 2))


def _size_label(attrs: list[dict]) -> str:
    return " ".join(str(a.get("value") or "") for a in attrs)


def parse_product(
    p: dict, roaster: Roaster, variation_prices: dict[str, float | None] | None = None
) -> CatalogItem:
    """variation_prices: variation id -> price, fetched separately because the listing only carries a range."""
    tags = sorted(
        {t["name"] for t in p.get("tags", []) if isinstance(t, dict)}
        | {c["name"] for c in p.get("categories", []) if isinstance(c, dict)}
    )
    variations = [v for v in p.get("variations") or [] if isinstance(v, dict)]
    stock = p.get("is_in_stock")
    if variations:
        variants = []
        for v in variations:
            label = _size_label(v.get("attributes") or [])
            price = (variation_prices or {}).get(str(v["id"]))
            variants.append(
                Variant(
                    variant_id=str(v["id"]),
                    title=label,
                    price_usd=price,
                    grams=parse_grams(label),
                    available=stock,
                )
            )
    else:
        name = html_to_text(p.get("name"))
        variants = [
            Variant(
                variant_id=str(p["id"]),
                title=name,
                price_usd=_price(p.get("prices")),
                grams=parse_grams(name),
                available=stock,
            )
        ]
    images = [i for i in p.get("images") or [] if isinstance(i, dict)]
    body = "\n".join(
        filter(None, [html_to_text(p.get("short_description")), html_to_text(p.get("description"))])
    )
    return CatalogItem(
        roaster_id=roaster.roaster_id,
        product_id=str(p["id"]),
        handle=p.get("slug") or str(p["id"]),
        url=p.get("permalink") or f"https://{roaster.host}/?p={p['id']}",
        title=html_to_text(p.get("name")),
        platform="woocommerce",
        product_type=p.get("type") or "",
        tags=tags,
        image=images[0].get("src") if images else None,
        variants=variants,
        body_text=body,
        payload_hash=payload_hash(
            {"product": p, "variation_prices": variation_prices} if variation_prices else p
        ),
    )


def _needs_variation_prices(p: dict) -> bool:
    rng = (p.get("prices") or {}).get("price_range") or {}
    return bool(p.get("variations")) and rng.get("min_amount") != rng.get("max_amount")


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    base = f"https://{roaster.host}/wp-json/wc/store/v1/products"
    raw: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = client.fetch_json(base, per_page=100, page=page)
        if not isinstance(data, list):
            raise FetchError(f"unexpected Store API payload from {roaster.host}")
        raw.extend(data)
        if len(data) < 100:
            break
    lookups = 0
    items = []
    for p in raw:
        prices: dict[str, float | None] = {}
        if _needs_variation_prices(p):
            for v in p["variations"]:
                if lookups >= MAX_VARIATION_LOOKUPS:
                    break
                lookups += 1
                try:
                    prices[str(v["id"])] = _price(client.fetch_json(f"{base}/{v['id']}").get("prices"))
                except (FetchError, AttributeError):
                    prices[str(v["id"])] = None  # unknown beats wrong: no per-gram price for this size
        else:
            single = _price(p.get("prices"))
            prices = {str(v["id"]): single for v in p.get("variations") or [] if isinstance(v, dict)}
        items.append(parse_product(p, roaster, prices))
    return items
