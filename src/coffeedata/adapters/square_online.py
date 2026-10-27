"""Square Online (editmysite.com) sites load their catalog from a public JSON API keyed by user and site ids,
which appear in the site's homepage HTML."""

from __future__ import annotations

import re

from ..http import FetchError, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams
from ..text import html_to_text, to_float

API = "https://cdn5.editmysite.com/app/store/api/v28/editor/users/{user}/sites/{site}/products"
MAX_PAGES = 20
_USER = re.compile(r'"user":\{"id":"?(\d+)')
_SITE = re.compile(r'"site_id":"?(\d{6,})')


def site_ids(html: str) -> tuple[str, str]:
    user, site = _USER.search(html), _SITE.search(html)
    if not (user and site):
        raise FetchError("no Square Online user/site id in homepage")
    return user.group(1), site.group(1)


def parse_product(p: dict, roaster: Roaster) -> CatalogItem:
    price = p.get("price") or {}
    low, high = to_float(price.get("regular_low")), to_float(price.get("regular_high"))
    name = (p.get("name") or "").strip()
    images = (p.get("images") or {}).get("data") or []
    sold_out = (p.get("badges") or {}).get("out_of_stock")
    return CatalogItem(
        roaster_id=roaster.roaster_id,
        product_id=str(p.get("site_product_id") or p["id"]),
        handle=(p.get("site_link") or "").split("/")[1]
        if "/" in (p.get("site_link") or "")
        else str(p["id"]),
        url=p.get("absolute_site_link") or f"https://{roaster.host}/{p.get('site_link', '')}",
        title=name,
        platform="square_online",
        product_type=p.get("product_type") or "",
        image=images[0].get("absolute_url") if images and isinstance(images[0], dict) else None,
        # The listing only has a price range; a single price is only trustworthy when low == high.
        variants=[
            Variant(
                variant_id=str(p["id"]),
                title=name,
                price_usd=low if low == high else None,
                grams=parse_grams(name),
                available=None if sold_out is None else not sold_out,
            )
        ],
        body_text=html_to_text(p.get("short_description")),
    )


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    r = client.get(f"https://{roaster.host}/")
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code} homepage")
    user, site = site_ids(r.text)
    items: list[CatalogItem] = []
    for page in range(1, MAX_PAGES + 1):
        data = client.fetch_json(API.format(user=user, site=site), page=page, per_page=60, include="images")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise FetchError("unexpected Square Online payload")
        items.extend(parse_product(p, roaster) for p in data["data"] if isinstance(p, dict))
        pages = ((data.get("meta") or {}).get("pagination") or {}).get("total_pages") or 1
        if page >= pages:
            break
    return items
