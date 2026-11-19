"""WooCommerce stores expose the public Store API at /wp-json/wc/store/v1/products."""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from collections.abc import Iterator

import httpx

from ..http import (
    Blocked,
    DeadlineReached,
    FetchError,
    PartialCatalog,
    PoliteClient,
    SiteDisallowed,
    block_status,
    causes,
    challenged,
)
from ..models import CatalogItem, Roaster, Variant, currency_code, parse_grams, payload_hash
from ..text import html_to_text

# No cap on pages or on variation prices: the whole catalog is read, or the fetch fails, or it is stored as partial
# (the run's deadline, or a count that differs from the store's X-WP-Total). The owner removed every cap on
# 2026-11-02: MAX_VARIATION_LOOKUPS = 150 left 48 of 201 stores' sizes unpriced, and MAX_PAGES = 20 cut
# villageofartisans.com at 2,000 products.
#
# The most a page holds, the Store API's documented maximum: `per_page` "Defaults to `10`. Maximum `100`."
# (docs/apis/store-api/README.md, "Pagination"; src/StoreApi/Routes/V1/Products.php, `per_page` maximum 100).
PER_PAGE = 100
# A page that times out is asked for again in smaller pages, these sizes in turn, and the rest of the catalog at the
# smallest that answered: volunteercoffee.com's 54 products embed 3,419 variations, 690 KB that took 41 s at 100 a
# page (the client gives up at 15 s), so it failed every week. Each size divides PER_PAGE and the one before it, so
# the products already read stay at the same offsets.
TIMEOUT_PER_PAGE = (25, 5)
# A bulk variations query the store rejects outright (an old Store API's 400 on type=variation, or a WAF's 403 that
# is no challenge): every size is asked for on its own instead, spaced as any request.
BULK_REJECTED_STATUSES = (400, 403)

log = logging.getLogger(__name__)


def _price(prices: dict | None) -> float | None:
    prices = prices or {}
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    return int(raw) / 10 ** int(prices.get("currency_minor_unit", 2))


def _currency(prices: dict | None) -> str | None:
    """The price's currency as the Store API states it: `prices.currency_code`, "Currency code (in ISO format) for
    returned prices" (src/StoreApi/Schemas/V1/AbstractSchema.php, get_store_currency_properties; added to every
    `prices` by ProductSchema::prepare_product_price_response through prepare_currency_response; "currency_code":
    "GBP" in docs/apis/store-api/resources-endpoints/products.md). aromacoffee.co.za states ZAR."""
    return currency_code((prices or {}).get("currency_code"))


def _term_names(parent_attrs: list) -> dict[str, dict[str, str]]:
    """The parent's attributes (ProductSchema::get_attributes): attribute name (lowercased) -> term slug -> term name.
    A taxonomy attribute's terms are {id, name, slug}; a custom one's have slug = name."""
    out: dict[str, dict[str, str]] = {}
    for a in parent_attrs or []:
        if not isinstance(a, dict):
            continue
        terms = out.setdefault(str(a.get("name") or "").lower(), {})
        for t in a.get("terms") or []:
            if isinstance(t, dict) and t.get("slug") not in (None, "") and t.get("name") not in (None, ""):
                terms[str(t["slug"])] = str(t["name"])
    return out


def _size_label(attrs: list[dict], parent_attrs: list | None = None) -> str:
    """A variation's readable size, from its attributes. The Store API lists each variation's attributes as
    {name, value}, where a taxonomy attribute's value is the term's slug ("12-oz"; ProductSchema::get_variations):
    the readable name ("12 oz") is the term's, in the parent's own `attributes[].terms` in the same response, matched
    by attribute name, then by slug. A value that matches no term (a custom attribute, whose slug is its name) is kept
    as it is; a null value ("any") is empty."""
    names = _term_names(parent_attrs or [])
    words = []
    for a in attrs:
        value = a.get("value")
        if value in (None, ""):
            words.append("")
            continue
        value = str(value)
        words.append(names.get(str(a.get("name") or "").lower(), {}).get(value, value))
    return " ".join(words)


def parse_product(
    p: dict,
    roaster: Roaster,
    variation_prices: dict[str, float | None] | None = None,
    variation_stock: dict[str, bool | None] | None = None,
    gone: set[str] | None = None,
    variation_currency: dict[str, str | None] | None = None,
) -> CatalogItem:
    """variation_prices: variation id -> price, fetched separately because the listing only carries a range.
    variation_stock: variation id -> its own is_in_stock, from the same lookup; a size without one takes the
    parent's (in stock when any size is). gone: variation ids whose own lookup answered 404 (the size no longer
    exists): left out of the product, never stored with a null price. variation_currency: variation id -> the
    currency its own price states, from the same lookup; a size priced from the listing, or whose lookup states
    none, takes the listing's."""
    tags = sorted(
        {t["name"] for t in p.get("tags", []) if isinstance(t, dict)}
        | {c["name"] for c in p.get("categories", []) if isinstance(c, dict)}
    )
    variations = [v for v in p.get("variations") or [] if isinstance(v, dict)]
    stock = p.get("is_in_stock")
    currency = _currency(p.get("prices"))
    if variations:
        variants = []
        for v in variations:
            if gone and str(v["id"]) in gone:
                continue
            label = _size_label(v.get("attributes") or [], p.get("attributes"))
            vid = str(v["id"])
            price = (variation_prices or {}).get(vid)
            own = (variation_stock or {}).get(vid)
            variants.append(
                Variant(
                    variant_id=vid,
                    title=label,
                    price_usd=price,
                    grams=parse_grams(label),
                    available=stock if own is None else own,
                    currency=(variation_currency or {}).get(vid) or currency,
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
                currency=currency,
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
        # The listing payload only: variation_prices depend on how the fetch went (a failed lookup), so hashing them
        # would change the hash of an unchanged product on a repeated fetch.
        payload_hash=payload_hash(p),
    )


def _needs_variation_prices(p: dict) -> bool:
    rng = (p.get("prices") or {}).get("price_range") or {}
    return bool(p.get("variations")) and rng.get("min_amount") != rng.get("max_amount")


def _variation_ids(p: dict) -> list[str]:
    return [str(v["id"]) for v in p.get("variations") or [] if isinstance(v, dict) and "id" in v]


def _timed_out(e: BaseException) -> bool:
    return any(isinstance(c, httpx.TimeoutException) for c in causes(e))


class _Listing:
    """What a Store API products query said about itself: X-WP-Total, the store's own count of the query's items."""

    def __init__(self) -> None:
        self.total: int | None = None


def _pages(
    client: PoliteClient, url: str, host: str, listing: _Listing | None = None, **params: str | int
) -> Iterator[list[dict]]:
    """Every page of a Store API products query, until a short page. A store that ignores `page` and repeats a page
    ends it too (nothing new would come). A page that times out is asked for again at the next smaller size
    (TIMEOUT_PER_PAGE), from the same offset. listing.total: the first page's X-WP-Total (never `Link`, which real
    stores send HTML-escaped: `page=2#038;per_page=1`)."""
    seen: set[str] = set()
    offset = 0
    per_page = PER_PAGE
    smaller = iter(TIMEOUT_PER_PAGE)
    while True:
        try:
            data, r = client.fetch_json_response(
                url, per_page=per_page, page=offset // per_page + 1, **params
            )
        except FetchError as e:
            if not _timed_out(e) or (per_page := next(smaller, 0)) == 0:
                raise
            continue
        if not isinstance(data, list):
            raise FetchError(f"unexpected Store API payload from {host}")
        if listing is not None and listing.total is None:
            total = r.headers.get("x-wp-total", "").strip()
            listing.total = int(total) if total.isdigit() else None
        ids = {str(p.get("id")) for p in data if isinstance(p, dict)}
        if data and ids <= seen:
            return
        seen |= ids
        yield data
        if len(data) < per_page:
            return
        offset += len(data)


def _batches(products: list[dict]) -> Iterator[list[dict]]:
    """Parents grouped so that their listed variations fill about one page each."""
    batch: list[dict] = []
    size = 0
    for p in products:
        n = len(_variation_ids(p))
        if batch and size + n > PER_PAGE:
            yield batch
            batch, size = [], 0
        batch.append(p)
        size += n
    if batch:
        yield batch


def _lookup(client: PoliteClient, base: str, vid: str) -> dict | None:
    """One variation, asked for on its own; None when the store answers 404: the size no longer exists (the store
    has no value for it), so it is dropped from the product. Any other failure fails the fetch (never a null price
    for a size the store prices): a challenge or a block (429, 430, 503 once the client's retries are spent) as such,
    so ingest.run counts it as a refusal or defers the store; anything else as the fetch's error, asked again next
    pass."""
    try:
        v = client.fetch_json(f"{base}/{vid}")
    except DeadlineReached:
        raise
    except FetchError as e:
        if challenged(e) or block_status(e):  # a refusal or a pause, not an unknown price (ingest.run)
            raise
        if e.status == 404 and not isinstance(e, Blocked):
            return None
        raise FetchError(f"variation {vid}: {e}", status=e.status) from e
    if not isinstance(v, dict):
        raise FetchError(f"variation {vid}: unexpected Store API payload")
    return v


def variation_prices(
    client: PoliteClient,
    base: str,
    host: str,
    products: list[dict],
    prices: dict[str, float | None],
    stock: dict[str, bool | None] | None = None,
    gone: set[str] | None = None,
    currencies: dict[str, str | None] | None = None,
) -> None:
    """Fills `prices` (variation id -> price), `stock` (variation id -> its own is_in_stock) and `currencies`
    (variation id -> the currency its price states) for every variation
    of `products`, in bulk: the Store API's products endpoint lists variations with their prices when asked for
    type=variation, filtered to their parents with `parent` (docs/apis/store-api/resources-endpoints/products.md,
    "Product Variations" and the `parent` and `type` parameters; ProductQuery::prepare_objects_query maps
    type=variation to post_type product_variation and `parent` to post_parent__in). About one request per 100
    variations, instead of one per variation.

    Each variation the bulk query returns is matched to the batch by its own `parent` id: one whose parent isn't in
    the batch means the store ignored the filter, so the rest of that batch is asked for one by one, and the next
    batch tries in bulk again. A published variation the parent doesn't list (an unpriced leftover the listing's
    get_visible_children leaves out) is simply not wanted: it never turns bulk off. A size the bulk query didn't
    return is asked for on its own. A store that rejects the bulk query outright (BULK_REJECTED_STATUSES, with no
    challenge) has every remaining size asked for on its own, spaced as usual. A size whose own lookup answers 404 no
    longer exists: it goes into `gone`, and is left out of its product. Any other failed request fails the fetch
    (_lookup): never a null price, or a null stock, for a size the store has."""
    stock = {} if stock is None else stock
    gone = set() if gone is None else gone
    currencies = {} if currencies is None else currencies
    wanted = {vid for p in products for vid in _variation_ids(p)}
    bulk = True
    for batch in _batches(products):
        if not bulk:
            break
        mine = {vid for p in batch for vid in _variation_ids(p)}
        ids = [str(p["id"]) for p in batch]
        parents = set(ids)
        try:
            for data in _pages(client, base, host, type="variation", parent=",".join(ids)):
                foreign = False
                for v in data:
                    if not isinstance(v, dict) or "id" not in v:
                        continue
                    if str(v.get("parent")) not in parents:
                        foreign = (
                            True  # the store ignored `parent` (or `type`): not a variation of this batch
                        )
                        continue
                    vid = str(v["id"])
                    if vid in wanted:
                        prices[vid] = _price(v.get("prices"))
                        stock[vid] = v.get("is_in_stock")
                        currencies[vid] = _currency(v.get("prices"))
                if (
                    foreign or mine <= prices.keys()
                ):  # every listed size priced: no need to ask for an empty page
                    break
        except FetchError as e:
            if (
                isinstance(e, (DeadlineReached, Blocked))
                or e.status not in BULK_REJECTED_STATUSES
                or challenged(e)
            ):
                raise
            log.info("%s: bulk variations query rejected (%s): every size asked for on its own", host, e)
            bulk = False
    for vid in sorted(wanted - prices.keys(), key=lambda x: (len(x), x)):
        one = _lookup(client, base, vid)
        if one is None:
            gone.add(vid)
            continue
        prices[vid] = _price(one.get("prices"))
        stock[vid] = one.get("is_in_stock")
        currencies[vid] = _currency(one.get("prices"))


# The Store API's path. A store whose robots.txt disallows it to us (Yoast's `Disallow: /wp-json/` and
# `Disallow: /?rest_route=` under `User-agent: *`: martinhenrycoffee.com, 2026-11-16) is read the way robots.txt does
# allow: its sitemap, then each product page's JSON-LD (adapters.generic), under the same rules and spacing.
STORE_API_PATH = "/wp-json/wc/store/v1/products"


# A WooCommerce product page, as WordPress and WooCommerce's own templates print it. The product's post id is the
# Store API's product id: WordPress's body class `postid-<id>` (get_body_class), WooCommerce's `<div id="product-<id>">`
# (content-single-product.php), or the `<link rel="shortlink" href="...?p=<id>">` WordPress prints (wp_shortlink_wp_head).
_POSTID = re.compile(r"<body\b[^>]*\bclass=[\"'][^\"']*\bpostid-(\d+)\b", re.I)
_PRODUCT_DIV = re.compile(r"<div\b[^>]*\bid=[\"']product-(\d+)[\"'][^>]*>", re.I)
_SHORTLINK = re.compile(
    r"<link\b(?=[^>]*\brel=[\"']shortlink[\"'])[^>]*\bhref=[\"'][^\"']*[?&](?:amp;)?p=(\d+)", re.I
)
# A variable product's sizes, inline in its add-to-cart form (single-product/add-to-cart/variable.php): each
# variation's id, attribute values, display_price and is_in_stock. `false` when the product has more variations than
# the store's woocommerce_ajax_variation_threshold (30 by default): the form then asks for each one by AJAX.
_VARIATIONS = re.compile(r"\bdata-product_variations=([\"'])(.*?)\1", re.S)
_CLASS = re.compile(r"\bclass=[\"']([^\"']*)[\"']", re.I)
# Its categories and tags (single-product/meta.php): the names the Store API lists as `categories` and `tags`.
_META = re.compile(r"<span\b[^>]*\bclass=[\"'](?:posted_in|tagged_as)[\"'][^>]*>(.*?)</span>", re.S | re.I)
_TAG_LINK = re.compile(r"<a\b[^>]*\brel=[\"']tag[\"'][^>]*>(.*?)</a>", re.S | re.I)


def post_id(page: str) -> str | None:
    """The product's post id on its own page (the Store API's product id), or None."""
    # The body class and the shortlink name the queried post itself; the product div comes before any related
    # product's in the template, and those are list items without an id (content-product.php).
    for rx in (_POSTID, _SHORTLINK, _PRODUCT_DIV):
        if m := rx.search(page):
            return m.group(1)
    return None


def page_variations(page: str) -> list[dict] | None:
    """The variations inline in the page's add-to-cart form, or None when it has none (a simple product, or more
    variations than the store shows inline)."""
    if not (m := _VARIATIONS.search(page)):
        return None
    try:
        data = json.loads(htmllib.unescape(m.group(2)))
    except ValueError:
        return None
    return (
        [v for v in data if isinstance(v, dict) and "variation_id" in v] if isinstance(data, list) else None
    )


def _product_classes(page: str, pid: str) -> list[str]:
    for m in _PRODUCT_DIV.finditer(page):
        if m.group(1) == pid and (c := _CLASS.search(m.group(0))):
            return c.group(1).split()
    return []


def parse_page(page: str, url: str, roaster: Roaster) -> CatalogItem | None:
    """A WooCommerce product page read in place of the Store API (a store whose robots.txt disallows it), with the
    Store API's identity, so the product keeps its coffee_id and its sizes their price series:
    - product_id: its post id (post_id), platform `woocommerce`;
    - a variable product's sizes: each variation's id, its attribute values joined by a space as the Store API
      answered them for this store (`ground-drip 12oz`; an `any` attribute is empty), display_price, is_in_stock;
    - a simple product: one size whose id is the product's, titled with its name, as parse_product does;
    - product_type (`simple`, `variable`...) from its `product-type-` class; tags from its category and tag links.
    A page without a post id keeps the JSON-LD's ids and platform `generic` (generic.page_item); a variable product
    whose sizes aren't inline keeps the JSON-LD's offers, under its post id."""
    from . import generic

    base = generic.page_item(page, url, roaster)
    if base is None or (pid := post_id(page)) is None:
        return base
    classes = _product_classes(page, pid)
    kind = next((c.removeprefix("product-type-") for c in classes if c.startswith("product-type-")), "")
    currency = next((v.currency for v in base.variants if v.currency), None)
    variations = page_variations(page)
    variants = base.variants
    if variations:
        variants = []
        for v in variations:
            attrs = a if isinstance(a := v.get("attributes"), dict) else {}
            label = " ".join(str(a or "") for a in attrs.values())
            stock = v.get("is_in_stock")
            variants.append(
                Variant(
                    variant_id=str(v["variation_id"]),
                    title=label,
                    price_usd=_to_price(v.get("display_price")),
                    grams=parse_grams(label),
                    available=stock if isinstance(stock, bool) else None,
                    currency=currency,
                )
            )
    elif kind in ("simple", "") and len(base.variants) == 1:
        one = base.variants[0]
        variants = [
            one.model_copy(update={"variant_id": pid, "title": base.title, "grams": parse_grams(base.title)})
        ]
    tags = sorted({html_to_text(t) for span in _META.findall(page) for t in _TAG_LINK.findall(span)} - {""})
    return base.model_copy(
        update={
            "product_id": pid,
            "platform": "woocommerce",
            "product_type": kind,
            "tags": tags,
            "variants": variants,
            "payload_hash": payload_hash({"jsonld": base.payload_hash, "variations": variations}),
        }
    )


def _to_price(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class _StoreApiDisallowed(Exception):
    """robots.txt disallows the Store API's first listing page itself: nothing was read from the API."""


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    try:
        return _store_api(client, roaster)
    except _StoreApiDisallowed:
        pass
    # Every page the generic path reads is checked against the same robots.txt (a product page it disallows is
    # Blocked like any other) and spaced the same; nothing here asks for the Store API again.
    from . import generic

    client.note_route(
        f"generic: robots.txt disallows the Store API ({STORE_API_PATH}); sitemap and product pages"
    )
    items = generic.read_pages(client, roaster, declared=True, parse=parse_page)
    if not items:
        # the sitemap listed no product page we may read: not an empty catalog, still the robots block it was
        raise Blocked(f"https://{roaster.host}{STORE_API_PATH} (and no product page in its sitemap)")
    return items


def _store_api(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    base = f"https://{roaster.host}{STORE_API_PATH}"
    raw: list[dict] = []
    prices: dict[str, float | None] = {}
    stock: dict[str, bool | None] = {}
    gone: set[str] = set()  # sizes whose own lookup answered 404: they no longer exist, and are left out
    currencies: dict[str, str | None] = {}
    listing = _Listing()
    try:
        for data in _pages(client, base, roaster.host, listing):
            for p in (p for p in data if isinstance(p, dict)):
                raw.append(p)
                if not _needs_variation_prices(p):  # one price for every size: the listing's
                    prices |= dict.fromkeys(_variation_ids(p), _price(p.get("prices")))
        needs = [p for p in raw if _needs_variation_prices(p)]
        variation_prices(client, base, roaster.host, needs, prices, stock, gone, currencies)
    except Blocked as e:
        # Only a block on the first listing page's own URL, before anything was read, turns into the sitemap path: a
        # store closed to us (SiteDisallowed) stays closed, and a block later on (a variation lookup) fails this fetch
        # as before, never throwing away what the API answered for a second read of the whole store.
        if raw or isinstance(e, SiteDisallowed) or str(e) != base:
            raise
        raise _StoreApiDisallowed(str(e)) from e
    except DeadlineReached as e:
        # Only products read whole: listed, with every size priced or known gone. The rest stay as they were.
        done = [
            parse_product(p, roaster, prices, stock, gone, currencies)
            for p in raw
            if prices.keys() | gone >= set(_variation_ids(p))
        ]
        raise PartialCatalog(f"{roaster.host}: {e} ({len(done)} of {len(raw)}+ products read)", done) from e
    items = [parse_product(p, roaster, prices, stock, gone, currencies) for p in raw]
    read = len({str(p.get("id")) for p in raw})
    if listing.total is not None and read != listing.total:
        # The store counts the query's products itself (X-WP-Total, docs/apis/store-api/README.md "Pagination"): a
        # read that differs ended early or ran into a changing catalog, so it is never the whole catalog.
        raise PartialCatalog(f"{roaster.host}: {read} products read, X-WP-Total says {listing.total}", items)
    return items
