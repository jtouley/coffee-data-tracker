"""Shopify storefronts publish their catalog at /products.json."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from itertools import count

import httpx

from ..http import (
    BLOCK_STATUSES,
    DeadlineReached,
    FetchError,
    PartialCatalog,
    PoliteClient,
    asked_wait,
    causes,
    store_wait,
)
from ..models import CatalogItem, Roaster, Variant, currency_code, parse_grams, payload_hash
from ..ratelimit import HostRateLimited, RateLimited
from ..text import html_to_text, to_float

log = logging.getLogger(__name__)

# No page cap: pages are read until the catalog ends (an empty page, a repeated page, or a short one below). kaldi.com
# lists more than 60 x 250 products: it failed the cap of 20 on 2026-10-31 and the cap of 60 on 2026-11-02. Only the
# run's deadline stops a catalog early, and then it is stored as partial.
# The most a page holds. The storefront's /products.json isn't documented on its own; Shopify's REST pagination docs
# give the `limit` it shares with the Admin API's product list: "The default `limit` value is `50`. The maximum
# `limit` value is `250`." (https://shopify.dev/docs/api/usage/pagination-rest, Parameters). A larger limit is
# answered with 250.
PAGE_LIMIT = 250
# A page with fewer than PAGE_LIMIT products ends the catalog, so the empty page after it is never asked for, but
# only once the products so far reach the store's last known count (its latest ok fetch). That empty page drew the
# 429s (B's retest, 2026-10-31: every "page 2" failure had its whole catalog on page 1). Shopify can return short
# pages with more behind them (it drops hidden products after paging): 3 of 1,034 stores in A's real crawl
# (portlandcoffeeroasters.com, talitha.com, onyxcoffeelab.com with 991 products). A store known to be bigger, or
# with no history, keeps paginating to the empty page. Exactly PAGE_LIMIT products on the last page costs one more
# request.
SHORT_PAGE_ENDS = True
# A store that answers a page with 429 (Retry-After 60) is not restarted from page 1: restarting hits the same wall
# every time (A's real crawl, 2026-10-31: 11 large stores served page 1, then 429 on page 2, and failed). The page
# raises PageBlocked with the products fetched so far; ingest.run backs off (max(Retry-After, 60 s), 120 s, 240 s)
# without holding a worker, then asks for that same page again (fetch(..., resume=...)).
#
# No sitemap pre-read: a Shopify sitemap index only lists product sitemaps of up to 5,000 URLs each, so the page
# count would need those child sitemaps too: more requests to the same per-store budget that answers 429.
BLOCK_DEFAULT_WAIT = 60.0  # a block whose Retry-After the client didn't pass on
# Refusals, not pauses: recorded as blocked, never backed off. A 430 (Shopify's IP-wide block) is a pause like a
# 429: the page is backed off and asked again, then the store parked for crawl-retry.
HARD_BLOCK_STATUSES = (401, 403)
# The prices' currency. /products.json gives each variant's `price` as a bare amount ("159.00"), with no currency.
# The response that carries it states it: the storefront sets its `cart_currency` cookie, the currency the cart, and
# so the storefront's prices, are in (https://www.shopify.com/legal/cookies: "cart_currency ... initialize a new
# empty cart with the same currency as the one just used"; the Ajax API returns "All monetary properties ... in the
# customer's presentment currency", https://shopify.dev/docs/api/ajax/reference/cart). Checked 2026-11-04 against
# the store's own product page JSON-LD: lacabra.com's products.json said 159.00 with cart_currency=DKK, and its page
# 159.00 DKK; pilotcoffeeroasters.com 26.75 with CAD, and CAD; onyxcoffeelab.com USD. A page whose response doesn't
# set it again carries the cookie the client sent with it, from an earlier page of the store. No extra request: the
# shop's own record (/meta.json, `currency`) is undocumented and states the shop's default currency, not the one
# a page is priced in, and the Ajax cart (/cart.js) is disallowed by Shopify's default robots.txt.
CURRENCY_COOKIE = "cart_currency"
_SENT_COOKIE = re.compile(rf"(?:^|;)\s*{CURRENCY_COOKIE}=([^;]*)")


class PageBlocked(RateLimited):
    """The store paused us on `page`: `items` are the products from the pages before it, `wait` its Retry-After."""

    def __init__(self, message: str, page: int, items: list[CatalogItem], wait: float, host: str) -> None:
        super().__init__(message)
        self.page = page
        self.items = items
        self.wait = wait
        self.host = host  # the host that blocked (after redirects)


class StoreBlocked(FetchError):
    """A 401 or 403: the store refuses. Never backed off or deferred."""


def _hard_block(e: FetchError) -> int | None:
    for c in causes(e):
        if isinstance(c, httpx.HTTPStatusError) and c.response.status_code in HARD_BLOCK_STATUSES:
            return c.response.status_code
    return next((s for s in HARD_BLOCK_STATUSES if str(e).startswith(f"HTTP {s} ")), None)


def _blocked_host(e: FetchError, default: str) -> str:
    for c in causes(e):
        if isinstance(c, HostRateLimited):
            return c.host
        if isinstance(c, httpx.HTTPStatusError):
            return c.request.url.host
    return default


def _block_wait(e: FetchError) -> float | None:
    """The store's Retry-After for this block (at most http.MAX_STORE_WAIT), or None if this failure isn't a store's
    pause."""
    if isinstance(e, HostRateLimited):
        return store_wait(e.wait)
    if isinstance(e, RateLimited):  # the platform as a whole gave up: not this store's pause
        return None
    for c in causes(e):  # the client's retries ran out on a block: fetch_json wraps the HTTPStatusError
        if isinstance(c, httpx.HTTPStatusError) and c.response.status_code in BLOCK_STATUSES:
            wait = asked_wait(c.response)
            return BLOCK_DEFAULT_WAIT if wait is None else store_wait(wait)
    return BLOCK_DEFAULT_WAIT if str(e).startswith(("HTTP 429 ", "HTTP 430 ", "HTTP 503 ")) else None


def _published_at(value: object) -> datetime | None:
    """One odd date must never cost a roaster its catalog: anything unreadable is None. No offset means UTC."""
    try:
        t = datetime.fromisoformat(str(value)) if value else None
        if t is None:
            return None
        return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError):
        return None


def page_currency(r: httpx.Response) -> str | None:
    """The currency a /products.json page is priced in, as the store states it (CURRENCY_COOKIE): the cookie the
    response sets, else the one its request sent. None when the store states neither."""
    for header in r.headers.get_list("set-cookie"):
        name, _, value = header.partition("=")
        if name.strip() == CURRENCY_COOKIE:
            return currency_code(value.split(";", 1)[0].strip())
    try:
        sent = _SENT_COOKIE.search(r.request.headers.get("cookie", ""))
    except RuntimeError:  # a response built without a request (tests)
        return None
    return currency_code(sent.group(1).strip()) if sent else None


def parse_product(p: dict, roaster: Roaster, currency: str | None = None) -> CatalogItem:
    """currency: the page's (page_currency), which every variant's price is in."""
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
                currency=currency,
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
        payload_hash=payload_hash(p),
    )


def fetch(
    client: PoliteClient, roaster: Roaster, resume: dict | None = None, known_products: int | None = None
) -> list[CatalogItem]:
    """The whole catalog. resume: {"page": n, "items": [...]} continues a catalog that was blocked on page n.
    known_products: the store's product count at its latest ok fetch, which lets a short page end the catalog."""
    items: dict[str, CatalogItem] = {i.product_id: i for i in (resume or {}).get("items", [])}
    for page in count((resume or {}).get("page", 1)):
        try:
            data, r = client.fetch_json_response(
                f"https://{roaster.host}/products.json", limit=PAGE_LIMIT, page=page
            )
        except DeadlineReached as e:
            # page: where a resume continues (ingest.run parks it), not page 1 again
            raise PartialCatalog(f"{e} (page {page})", list(items.values()), page=page) from e
        except FetchError as e:
            if (status := _hard_block(e)) is not None:
                raise StoreBlocked(f"blocked: HTTP {status} on page {page} of {roaster.host}") from e
            if (wait := _block_wait(e)) is None:
                raise
            msg = f"{roaster.host} blocked on page {page}: {e}"
            raise PageBlocked(msg, page, list(items.values()), wait, _blocked_host(e, roaster.host)) from e
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise FetchError(f"unexpected /products.json payload from {roaster.host}")
        # An empty page ends the catalog, and so does a store that ignores `page` and repeats itself, or (with
        # SHORT_PAGE_ENDS) a page shorter than the limit once the store's last known count is reached.
        currency = page_currency(r)
        new = {i.product_id: i for i in (parse_product(p, roaster, currency) for p in data["products"])}
        if not new.keys() - items.keys():
            break
        items |= new
        if (
            SHORT_PAGE_ENDS
            and len(data["products"]) < PAGE_LIMIT
            and known_products is not None
            and len(items) >= known_products
        ):
            break
    return list(items.values())
