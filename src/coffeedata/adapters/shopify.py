"""Shopify storefronts publish their catalog at /products.json."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx

from ..http import BLOCK_STATUSES, FetchError, PoliteClient
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..ratelimit import HostRateLimited, RateLimited, retry_after_seconds
from ..text import html_to_text, to_float

log = logging.getLogger(__name__)

MAX_PAGES = 20
PAGE_LIMIT = 250  # the most /products.json returns per page
# A store that serves page 1 and then answers 429 (Retry-After 60) for the next page: wait it out and ask for that
# page again, keeping the pages already fetched. Restarting from page 1 hits the same wall every time (A's real crawl,
# 2026-10-31: 11 large stores failed that way). At most this many waits per page, each at most MAX_PAGE_WAIT s.
PAGE_WAITS = 3
MAX_PAGE_WAIT = 120.0
BLOCK_DEFAULT_WAIT = 60.0  # a block whose Retry-After the client didn't pass on


def _block_wait(e: FetchError) -> float | None:
    """Seconds to wait before asking for the same page again, or None if this failure isn't a store's pause."""
    if isinstance(e, HostRateLimited):
        return min(MAX_PAGE_WAIT, max(1.0, e.wait))
    if isinstance(e, RateLimited):  # the platform as a whole gave up: don't hold a worker
        return None
    c: BaseException | None = e
    while c is not None:  # the client's retries ran out on a block: fetch_json wraps the HTTPStatusError
        if isinstance(c, httpx.HTTPStatusError) and c.response.status_code in BLOCK_STATUSES:
            wait = retry_after_seconds(c.response.headers.get("retry-after"))
            return min(MAX_PAGE_WAIT, BLOCK_DEFAULT_WAIT if wait is None else max(1.0, wait))
        c = c.__cause__
    if str(e).startswith(("HTTP 429 ", "HTTP 430 ", "HTTP 503 ")):
        return BLOCK_DEFAULT_WAIT
    return None


def _page(client: PoliteClient, roaster: Roaster, page: int) -> object:
    for attempt in range(PAGE_WAITS + 1):
        try:
            return client.fetch_json(f"https://{roaster.host}/products.json", limit=PAGE_LIMIT, page=page)
        except FetchError as e:
            # Page 1 fails as before: a store that never serves a page is parked or refused by ingest.run.
            wait = _block_wait(e) if page > 1 else None
            if wait is None or attempt == PAGE_WAITS:
                raise
            log.info(
                "%s: blocked on page %d (%s); waiting %.0fs, then that page again",
                roaster.host,
                page,
                e,
                wait,
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


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
        payload_hash=payload_hash(p),
    )


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    items: dict[str, CatalogItem] = {}
    for page in range(1, MAX_PAGES + 1):
        data = _page(client, roaster, page)
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise FetchError(f"unexpected /products.json payload from {roaster.host}")
        # A full page can hold fewer than 250 products (Shopify drops some after paging; onyxcoffeelab.com's pages
        # hold 249, 247, 234, 242), so only an empty page ends the catalog. A store that ignores `page` and
        # repeats itself ends it too.
        new = {i.product_id: i for i in (parse_product(p, roaster) for p in data["products"])}
        if not new.keys() - items.keys():
            break
        items |= new
    else:  # never reached an empty page: storing this as the whole catalog would be a silent truncation
        raise FetchError(f"{roaster.host}: still more products after {MAX_PAGES} pages")
    return list(items.values())
