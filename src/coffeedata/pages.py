"""Secondary crawler for stores whose JSON feed rate-limits us: the sitemap first, then product pages.

Used for stores whose JSON feed (/products.json, the Store API) answered this run only with rate limiting (429s),
never a block (401, 403, 430): see rate_limited(). It yields the same CatalogItem records the JSON adapters do: for Shopify
the same product and variant ids, so a coffee keeps its coffee_id whichever path read it.

Cheapest source first, per store:
1. robots.txt, then sitemap.xml and its product sitemaps: what exists, with each product's title where the
   sitemap carries one (Shopify's image:title), so merch and gear are skipped without fetching their pages.
2. Each remaining product page, once: schema.org Product JSON-LD, and the product JSON the platform embeds
   (Shopify's analytics `meta`, a theme's product JSON, WooCommerce's variation form).
3. Collection pages, only when the sitemap lists no products.
4. A headless browser, only when asked (`render=True`), for a page that loaded but carried no product data, or a
   coffee whose page showed no fact lines (a spec panel that scripts build).

Rules: the crawler's own User-Agent with a contact address; robots.txt is honored; requests to a store are spaced
PAGE_INTERVAL apart; nothing is retried after a block. A 401, 403, 429, 430 or 503, or a page that reads as a
CAPTCHA or bot challenge, stops the store at once (Refused): it is never solved, waited out or worked around.

Facts only: the stored body text is the product's labelled fact lines (origin, process, variety, roast, notes,
elevation, producer), never its description; no images.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import unescape
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from . import ingest
from .adapters import generic, shopify
from .adapters.generic import _PRODUCTISH, jsonld_products
from .classify import excluded, is_coffee
from .http import USER_AGENT, Blocked, FetchError, PoliteClient
from .lake import Lake, new_batch_id
from .models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from .sitemap import Entry, entries, product_sitemaps
from .text import html_to_text, scrub_surrogates, to_float

log = logging.getLogger(__name__)

CONTACT = os.environ.get("SUPPORT_EMAIL", "support@registry.coffee")
PAGES_USER_AGENT = f"{USER_AGENT.rstrip(')')}; {CONTACT})"
PAGE_INTERVAL = 3.0  # seconds between requests to one store: product pages are heavier than a JSON page
MAX_PAGES = (
    150  # product pages per store per run; more candidates than this fails the store (no silent truncation)
)
MAX_SITEMAPS = 5
COLLECTION_PATHS = ("/collections/all", "/shop/")
COLLECTION_PAGES = 5

REFUSAL_STATUSES = (401, 403, 429, 430, 503)
# Markers of a bot challenge, CAPTCHA or block page served with a 200. Matched on the raw HTML.
_CHALLENGE = re.compile(
    r"/cdn-cgi/challenge-platform|cf-chl|cf-browser-verification|<title>\s*(just a moment|attention required|"
    r"access denied|are you a robot)|captcha-delivery\.com|px-captcha|perimeterx|_incapsula_resource|"
    r"hcaptcha\.com/1/api|verify you are (a )?human",
    re.I,
)
# A page with any of these carries product data, so it isn't a block page (whatever its newsletter form loads).
_PRODUCT_MARKUP = re.compile(
    r"application/ld\+json|var meta\s*=|data-product-json|data-product_variations", re.I
)


class Refused(FetchError):
    """The store refused or challenged us. The store's crawl stops here; nothing is retried or bypassed."""


def client(interval: float = PAGE_INTERVAL, **kw: object) -> PoliteClient:
    """A PoliteClient for page crawling: its own User-Agent with a contact address, store-level spacing, no retries
    (a block is an answer, not something to wait out)."""
    return PoliteClient(min_interval=interval, retries=1, user_agent=PAGES_USER_AGENT, **kw)  # type: ignore[arg-type]


# --- 1. sitemap ------------------------------------------------------------------------------------------------


def _page(c: PoliteClient, url: str) -> httpx.Response | None:
    """A 200 response; None for 404/410 (gone); Refused on a block or challenge; FetchError otherwise."""
    try:
        r = c.get(url)
    except Blocked:
        raise
    except httpx.HTTPStatusError as e:  # PoliteClient raises for retry statuses (429, 430, 5xx)
        r = e.response
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__} {url}") from e
    if r.status_code in REFUSAL_STATUSES:
        raise Refused(f"HTTP {r.status_code} {url}")
    if r.status_code in (404, 410):
        return None
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code} {url}")
    if is_challenge(r.text):
        raise Refused(f"challenge page {url}")
    return r


def is_challenge(html: str) -> bool:
    """A block page, bot challenge or CAPTCHA served in place of the page: a challenge marker and no product data."""
    return bool(_CHALLENGE.search(html)) and not _PRODUCT_MARKUP.search(html)


def sitemap_entries(c: PoliteClient, host: str) -> list[Entry]:
    """Product-looking URLs from sitemap.xml, following its product sitemaps. [] if the site has none."""
    r = _page(c, f"https://{host}/sitemap.xml")
    if r is None:
        return []
    xml = r.text
    if "<sitemapindex" in xml:
        subs = [_page(c, u) for u in product_sitemaps(xml)[:MAX_SITEMAPS]]
        xml = "".join(sub.text for sub in subs if sub is not None)
    return [e for e in entries(xml) if _PRODUCTISH.search(urlparse(e.url).path)]


_HREF = re.compile(r'href="([^"#?]+)"', re.I)


def collection_entries(c: PoliteClient, host: str) -> list[Entry]:
    """Fallback when the sitemap lists no products: product links from the store's all-products listing."""
    for path in COLLECTION_PATHS:
        found: dict[str, Entry] = {}
        for n in range(1, COLLECTION_PAGES + 1):
            url = f"https://{host}{path}"
            if n > 1:
                url += f"?page={n}" if path.startswith("/collections") else f"page/{n}/"
            try:
                r = _page(c, url)
            except Blocked:
                break
            if r is None:
                break
            new = {
                u: Entry(u)
                for h in _HREF.findall(r.text)
                if urlparse(u := urljoin(url, h)).netloc == host and _PRODUCTISH.search(urlparse(u).path)
            }
            if not new.keys() - found.keys():
                break
            found |= new
        if found:
            return list(found.values())
    return []


def candidates(entries: list[Entry]) -> list[Entry]:
    """The entries worth a page fetch: everything except products whose sitemap title already says they aren't
    coffee (gear, merch, subscriptions...). Untitled entries are always kept."""
    keep = []
    for e in entries:
        stub = CatalogItem(roaster_id="", product_id="", handle="", url=e.url, title=e.title, platform="")
        if e.title and excluded(stub):
            continue
        keep.append(e)
    return keep


# --- 2. product pages ------------------------------------------------------------------------------------------

_JSON_SCRIPT = re.compile(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', re.S | re.I)
_SHOPIFY_META = re.compile(r"var meta\s*=\s*(\{.*?\});\s*(?:\n|for|window|var|$)", re.S)
_WOO_VARIATIONS = re.compile(r'data-product_variations="([^"]*)"')
_WOO_ID = re.compile(r"\bpostid-(\d+)\b|[?&]p=(\d+)['\"]")
_FACT_LABEL = (
    r"origin|country|region|process(?:ing)?|method|variet(?:y|ies|als?)|cultivars?|roast(?: level)?|"
    r"(?:tasting |flavou?r |cup(?:ping)? )?notes|we taste|tastes? like|elevation|altitude|producers?|farm|"
    r"washing station|harvest"
)
_FACT_LINE = re.compile(rf"^\s*({_FACT_LABEL})\s*[:\-–]\s*\S.{{0,150}}$", re.I)
_FACT_HEADING = re.compile(rf"^\s*({_FACT_LABEL})\s*:?\s*$", re.I)
_BLOCK = re.compile(r"<\s*/?(?:dt|dd|tr|td|th|li|p|div|h\d|br|dl|ul|ol|table|section)\b[^>]*>", re.I)


def _loads(s: str) -> object:
    try:
        return scrub_surrogates(json.loads(s))
    except ValueError:
        return None


def _product_json(html: str) -> dict | None:
    """A full product object a theme embeds (Shopify's /products/<handle>.js shape: id, handle, variants with
    `available`, tags, type, vendor), if the page has one."""
    for block in _JSON_SCRIPT.findall(html):
        d = _loads(block.strip())
        for p in (d, d.get("product") if isinstance(d, dict) else None):
            if (
                isinstance(p, dict)
                and p.get("id")
                and p.get("handle")
                and isinstance(p.get("variants"), list)
            ):
                return p
    return None


def _shopify_meta(html: str) -> dict | None:
    """Shopify's analytics object, on every Shopify storefront page: product id, type, vendor and each variant's id,
    price (in cents), title and SKU. It has no stock."""
    if m := _SHOPIFY_META.search(html):
        d = _loads(m.group(1))
        p = d.get("product") if isinstance(d, dict) else None
        if isinstance(p, dict) and p.get("id"):
            return p
    return None


def _dollars(cents: object) -> str | None:
    """Page JSON prices are integer cents; /products.json's are dollar strings."""
    return f"{cents / 100:.2f}" if isinstance(cents, int) and not isinstance(cents, bool) else None


def facts_text(html: str) -> str:
    """Only the labelled fact lines of a page ("Origin: Huila, Colombia", "Process" then "Washed"): what extract.rules
    reads. The description itself is never kept."""
    page = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    page = _BLOCK.sub("\n", page)  # spec tables and lists: one cell per line
    lines = html_to_text(page).splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if _FACT_LINE.match(line):
            out.append(line.strip())
        elif _FACT_HEADING.match(line) and i + 1 < len(lines) and len(lines[i + 1]) <= 120:
            out.append(f"{line.strip().rstrip(':')}: {lines[i + 1].strip()}")
    return "\n".join(dict.fromkeys(out))  # each fact once, in page order


def _offers(ld: dict) -> list[dict]:
    offers = ld.get("offers") or []
    if isinstance(offers, dict):
        offers = offers.get("offers") or [offers]
    return [o for o in offers if isinstance(o, dict)]


def _offer_available(o: dict) -> bool | None:
    a = str(o.get("availability") or "")
    return ("InStock" in a or "PreOrder" in a or "LimitedAvailability" in a) if a else None


def _as_products_json(full: dict | None, meta: dict | None, ld: dict, handle: str) -> dict:
    """A Shopify page's product data in /products.json's shape, so shopify.parse_product reads it exactly as it
    reads the feed. From a theme's product JSON when there is one; otherwise from `meta` (ids, prices) with each
    variant's stock from the JSON-LD offer that links to it (?variant=<id>) or shares its SKU."""
    if full:
        variants = [
            v | {"price": _dollars(v.get("price")) or v.get("price"), "option1": v.get("public_title")}
            for v in full["variants"]
            if isinstance(v, dict)
        ]
        return {
            "id": full["id"],
            "handle": full.get("handle") or handle,
            "title": full.get("title") or ld.get("name") or handle,
            "vendor": full.get("vendor"),
            "product_type": full.get("type") or full.get("product_type"),
            "tags": full.get("tags") or [],
            "published_at": full.get("published_at"),
            "variants": variants,
        }
    assert meta is not None
    offers = _offers(ld)
    by_vid = {parse_qs(urlparse(str(o.get("url") or "")).query).get("variant", [""])[0]: o for o in offers}
    by_sku = {str(o["sku"]): o for o in offers if o.get("sku")}
    variants = []
    for v in meta.get("variants") or []:
        if not isinstance(v, dict):
            continue
        o = (
            by_vid.get(str(v.get("id")))
            or by_sku.get(str(v.get("sku") or ""))
            or (offers[0] if len(offers) == 1 else {})
        )
        title = v.get("public_title") or "Default Title"
        variants.append(
            {
                "id": v.get("id"),
                "title": title,
                "option1": title,
                "price": _dollars(v.get("price")),
                "available": _offer_available(o),
            }
        )
    return {
        "id": meta["id"],
        "handle": meta.get("handle") or handle,
        "title": ld.get("name") or handle,
        "vendor": meta.get("vendor"),
        "product_type": meta.get("type"),
        "variants": variants,
    }


def parse_page(html: str, url: str, roaster: Roaster) -> CatalogItem | None:
    """One product page as a CatalogItem, or None if it carries no product data. Shopify pages go through
    shopify.parse_product and everything else through generic.parse_jsonld, the parsers the JSON and sitemap
    adapters use; only the stored text (facts only) and the image (none) differ."""
    full, meta = _product_json(html), _shopify_meta(html)
    lds = jsonld_products(html)
    ld = lds[0] if lds else {}
    if not (full or meta or ld):
        return None
    handle = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if full or meta:
        raw: dict = _as_products_json(full, meta, ld, handle)
        item = shopify.parse_product(raw, roaster)
    else:
        raw = ld
        item = generic.parse_jsonld(ld, url, roaster)
        update: dict = {"platform": roaster.adapter}
        # WooCommerce: the post id and variation ids the Store API uses, so a coffee keeps its coffee_id.
        if woo := _WOO_ID.search(html):
            update["product_id"] = woo.group(1) or woo.group(2)
        if variations := _woo_variations(html):
            update["variants"] = variations
        item = item.model_copy(update=update)
    facts = facts_text(html)
    return CatalogItem(
        **item.model_dump(exclude={"body_hash", "image", "payload_hash"}),
        image=None,  # no photos
        body_text=facts,  # labelled facts only, never the description
        payload_hash=payload_hash({"product": raw, "facts": facts}),
    )


def _woo_variations(page: str) -> list[Variant]:
    """WooCommerce's variation form: each variation's id (the Store API's variation id), price, stock and size."""
    m = _WOO_VARIATIONS.search(page)
    data = _loads(unescape(m.group(1))) if m else None  # the attribute is HTML-escaped JSON
    out = []
    for v in data if isinstance(data, list) else []:
        if not isinstance(v, dict) or not v.get("variation_id"):
            continue
        label = " ".join(str(a) for a in (v.get("attributes") or {}).values() if a)
        out.append(
            Variant(
                variant_id=str(v["variation_id"]),
                title=label,
                price_usd=to_float(v.get("display_price")),
                grams=parse_grams(label),
                available=v.get("is_in_stock"),
            )
        )
    return out


# --- 3. the store ----------------------------------------------------------------------------------------------

Renderer = Callable[[str], str]


@dataclass
class Report:
    """What one store's page crawl did, for the POC's live table."""

    source: str = ""  # sitemap, collections or none
    listed: int = 0  # product URLs found
    skipped_not_coffee: int = 0  # skipped on the sitemap title alone
    fetched: int = 0  # product pages requested
    parsed: int = 0  # pages that yielded a product
    rendered: int = 0  # pages sent to the browser
    facts_from_render: int = 0  # of those, coffees whose fact lines only the rendered page had


def fetch(
    c: PoliteClient, roaster: Roaster, render: Renderer | None = None, report: Report | None = None
) -> list[CatalogItem]:
    """The store's catalog from its product pages. Raises Refused on any block (stop the store), FetchError when any
    page couldn't be read (a partial catalog would make the missing products look delisted)."""
    rep = report if report is not None else Report()
    entries = sitemap_entries(c, roaster.host)
    rep.source = "sitemap"
    if not entries:
        entries, rep.source = collection_entries(c, roaster.host), "collections"
    if not entries:
        rep.source = "none"
        raise FetchError(f"{roaster.host}: no product URLs in the sitemap or collection pages")
    rep.listed = len(entries)
    todo = candidates(entries)
    rep.skipped_not_coffee = len(entries) - len(todo)
    if len(todo) > MAX_PAGES:
        raise FetchError(f"{roaster.host}: {len(todo)} product pages, more than MAX_PAGES={MAX_PAGES}")
    items: dict[str, CatalogItem] = {}
    failed = 0
    for e in todo:
        rep.fetched += 1
        try:
            r = _page(c, e.url)
        except Refused:
            raise
        except FetchError:  # a robots block on one product or a network error: this page only
            failed += 1
            continue
        if r is None:
            continue  # listed but gone: delisted, not unreadable
        item = parse_page(r.text, e.url, roaster)
        # Scripts can build what the server HTML lacks: the product data itself, or a coffee's spec panel (origin,
        # process...). Only then, and only when asked, the page is rendered.
        if render is not None and (item is None or (not item.body_text and is_coffee(item))):
            rep.rendered += 1
            html = render(e.url)
            if is_challenge(html):
                raise Refused(f"challenge page (rendered) {e.url}")
            rendered = parse_page(html, e.url, roaster)
            if rendered is not None and (item is None or rendered.body_text):
                item = rendered
                rep.facts_from_render += bool(rendered.body_text)
        if item is None:
            failed += 1
            continue
        rep.parsed += 1
        items.setdefault(item.product_id, item)
    if failed:
        raise FetchError(f"{failed} of {len(todo)} product pages unreadable")
    return list(items.values())


def browser_renderer(c: PoliteClient) -> Renderer:
    """A Playwright renderer for pages whose product data only appears after scripts run. Same User-Agent (no
    disguise), robots.txt checked through the client first, images, media and fonts not loaded. Lazily started."""
    state: dict = {}

    def render(url: str) -> str:
        if not c._allowed(url):
            raise Blocked(url)
        if "page" not in state:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

            state["pw"] = sync_playwright().start()
            state["browser"] = state["pw"].chromium.launch()
            ctx = state["browser"].new_context(user_agent=c.user_agent)
            ctx.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_()
                ),
            )
            state["page"] = ctx.new_page()
        c._wait_turn(urlparse(url).netloc)
        resp = state["page"].goto(url, wait_until="networkidle", timeout=30_000)
        if resp is not None and resp.status in REFUSAL_STATUSES:
            raise Refused(f"HTTP {resp.status} {url} (rendered)")
        return str(state["page"].content())

    def close() -> None:
        if "browser" in state:
            state["browser"].close()
            state["pw"].stop()

    render.close = close  # type: ignore[attr-defined]
    return render


# --- 4. a run over the lake ------------------------------------------------------------------------------------


BLOCK_SIGNS = (
    401,
    403,
    430,
)  # on the JSON feed: a block, not rate limiting. Such a store is never page-crawled.


def rate_limited(lake: Lake, run_date: str) -> list[Roaster]:
    """Roasters whose JSON feed this run answered only with rate limiting: at least one 429, no catalog page, no ok
    fetch, and never a 401, 403 or 430 (those are blocks, and a blocked store is left alone)."""
    from . import registry

    con = lake.connect()
    rows = con.execute(
        f"""
        select roaster_id from {lake.scan("fetches")} where run_date = ?
        group by roaster_id
        having count(*) filter (where status = 'ok') = 0
           and sum(rate_limited) > 0
           and sum(pages) = 0
           and not bool_or(list_has_any([s.status for s in coalesce(http_statuses, [])], ?))
        """,
        [date.fromisoformat(run_date), list(BLOCK_SIGNS)],
    ).fetchall()
    want = {r[0] for r in rows}
    return [s.roaster for s in registry.current(lake) if s.roaster.roaster_id in want]


def run(
    lake: Lake, roasters: list[Roaster], run_date: str, render: bool = False, interval: float = PAGE_INTERVAL
) -> tuple[dict, list[tuple[Roaster, Report, ingest.Result]]]:
    """Crawl `roasters` one after another (each store's own spacing; never two stores' pages at once) and append
    their records to the lake in the JSON adapters' shape: snapshots, fetches, enrichments. The fetches row is a
    second attempt for the run, so a store reads ok if either path succeeded. Bodies are the facts lines only."""
    rd = date.fromisoformat(run_date)
    out: list[tuple[Roaster, Report, ingest.Result]] = []
    with client(interval) as c:
        renderer = browser_renderer(c) if render else None
        try:
            for roaster in roasters:
                res = ingest.Result(roaster, datetime.now(UTC).replace(microsecond=0))
                rep = Report()
                start = time.monotonic()
                with c.track() as res.stats:
                    try:
                        res.items = fetch(c, roaster, renderer, rep)
                    except Exception as e:  # noqa: BLE001 - this store's error, never the run's
                        res.error = f"pages: {type(e).__name__}: {e}"[:300]
                res.duration_s = round(time.monotonic() - start, 3)
                log.info("%-32s %s", roaster.roaster_id, res.error or f"{len(res.items)} products")
                out.append((roaster, rep, res))
        finally:
            if renderer is not None:
                renderer.close()  # type: ignore[attr-defined]
    con = lake.connect()
    known = {r[0] for r in con.execute(f"select distinct body_hash from {lake.scan('bodies')}").fetchall()}
    enriched = {
        tuple(r)
        for r in con.execute(
            f"select distinct roaster_id, product_id, body_hash from {lake.scan('enrichments')}"
        ).fetchall()
    }
    batch = new_batch_id()
    for table, rows in ingest.rows_for([r for *_, r in out], rd, batch, known, enriched).items():
        lake.write_partition(table, run_date, batch, rows)
    summary = {
        "run_date": run_date,
        "batch_id": batch,
        "stores": len(out),
        "ok": sum(not r.error for *_, r in out),
        "products": sum(len(r.items) for *_, r in out),
        "requests": sum(r.stats.requests for *_, r in out),
    }
    return summary, out
