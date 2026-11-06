"""Square Online (editmysite.com) sites load their catalog from a public JSON API keyed by user and site ids.

The ids appear in the homepage in one of three forms: Square Online's own JSON, the variables an older Weebly site
(Square owns Weebly) sets, or an image upload path. Some roasters' own sites only link to a separate
<name>.square.site store, so that store's homepage is tried next.
"""

from __future__ import annotations

import re
from itertools import count

from ..http import DeadlineReached, FetchError, PartialCatalog, PoliteClient
from ..models import CatalogItem, Roaster, Variant, currency_code, parse_grams, payload_hash
from ..text import html_to_text, to_float

API = "https://cdn5.editmysite.com/app/store/api/v28/editor/users/{user}/sites/{site}/products"
# Page size: the storefront's own. This products API is the one Square Online's store pages call; Square documents
# no page size for it (it isn't one of Square's public APIs, whose page sizes the API reference gives per endpoint:
# https://developer.squareup.com/docs/build-basics/common-api-patterns/pagination). The storefront asks for
# per_page=60 (the call quoted on Square's developer forum, https://developer.squareup.com/forums/t/4008), so that
# is the most this crawl asks for: a larger page is untested against the store, and every page is read anyway.
PER_PAGE = 60
_ID_FORMS = [
    (re.compile(r'"user":\{"id":"?(\d+)'), re.compile(r'"site_id":"?(\d{6,})')),  # Square Online
    (
        re.compile(r"com_userID\s*=\s*[\"']?(\d+)"),
        re.compile(r"com_currentSite\s*=\s*[\"']?(\d{6,})"),
    ),  # Weebly
]
_UPLOAD_PATH = re.compile(r"/uploads/[0-9a-z]/(\d{5,})-(\d{12,})/")  # Weebly image uploads: <user>-<site>
# The host must end at square.site, so "x.square.site.example.com" doesn't count.
_STORE_LINK = re.compile(r"https?://([a-z0-9-]+)\.square\.site(?=[/:?#\"'\s<>]|$)", re.I)
# The store's currency. The products API states none: its `price` has amounts, subunits and a formatted string
# ("$9.50") but no currency code (whiffroasters.com, checked 2026-11-04). The store's own page states it, in the
# JSON Square Online embeds for the storefront: "storeInfo":{"merchant_id":...,"currency":"USD","locale":"en_US",...}.
# That JSON isn't documented; the field matches the Square merchant's currency, "The currency used for all
# transactions at this location, in ISO 4217 format" (https://developer.squareup.com/reference/square/objects/
# Location, `currency`). Its keys hold plain values or one level of nested objects before `currency`.
_STORE_CURRENCY = re.compile(r'"storeInfo"\s*:\s*\{(?:[^{}]|\{[^{}]*\})*?"currency"\s*:\s*"([^"]*)"')


def site_ids(html: str) -> tuple[str, str] | None:
    """Ids the page declares for its own store."""
    for user_re, site_re in _ID_FORMS:
        user, site = user_re.search(html), site_re.search(html)
        if user and site:
            return user.group(1), site.group(1)
    return None


def upload_ids(html: str) -> tuple[str, str] | None:
    """Ids from a Weebly image path. Weaker evidence (an image can come from another account), so last resort."""
    m = _UPLOAD_PATH.search(html)
    return (m.group(1), m.group(2)) if m else None


def store_links(html: str) -> list[str]:
    """Every distinct <name>.square.site store the page links to, in page order."""
    names = dict.fromkeys(m.group(1).lower() for m in _STORE_LINK.finditer(html))
    return [f"https://{n}.square.site" for n in names]


def store_currency(html: str) -> str | None:
    """The currency the store's page states (storeInfo.currency), as given; None when the page states none (an
    older Weebly site's page has no storeInfo)."""
    m = _STORE_CURRENCY.search(html)
    return currency_code(m.group(1)) if m else None


def parse_product(
    p: dict, roaster: Roaster, base: str | None = None, currency: str | None = None
) -> CatalogItem:
    """currency: the store's (store_currency), which its prices are in."""
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
        url=p.get("absolute_site_link") or f"{base or 'https://' + roaster.host}/{p.get('site_link', '')}",
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
                currency=currency,
            )
        ],
        body_text=html_to_text(p.get("short_description")),
        payload_hash=payload_hash(p),
    )


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    r = client.get(f"https://{roaster.host}/")
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code} homepage", status=r.status_code)
    base, ids = f"https://{roaster.host}", site_ids(r.text)
    currency = store_currency(r.text)
    for store in [] if ids else store_links(r.text):
        sr = client.get(f"{store}/")
        if sr.status_code == 200 and (ids := site_ids(sr.text)):
            base = store  # product links fall back to the store that serves them
            currency = store_currency(sr.text) or currency  # the page of the store whose ids are read
            break
    ids = ids or upload_ids(r.text)
    if ids is None:
        raise FetchError("no Square Online store ids on the homepage or a linked square.site store")
    user, site = ids
    items: list[CatalogItem] = []
    for page in count(1):  # every page the API says it has: no page cap (it was 20 x 60)
        try:
            data = client.fetch_json(
                API.format(user=user, site=site), page=page, per_page=PER_PAGE, include="images"
            )
        except DeadlineReached as e:
            raise PartialCatalog(f"{e} (page {page})", items) from e
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise FetchError("unexpected Square Online payload")
        items.extend(parse_product(p, roaster, base, currency) for p in data["data"] if isinstance(p, dict))
        pages = ((data.get("meta") or {}).get("pagination") or {}).get("total_pages") or 1
        if page >= pages or not data["data"]:
            break
    return items
