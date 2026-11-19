"""A WooCommerce store whose robots.txt disallows the Store API (martinhenrycoffee.com, run 2026-11-16: Yoast's
`Disallow: /wp-json/` and `Disallow: /?rest_route=` under `User-agent: *`) is read from its sitemap (or the one its
robots.txt names) and product pages, under the same rules and spacing. Split from #178: it changes these products'
ids (CLAUDE.md step 9). No test sends a request anywhere: every response comes from an httpx.MockTransport."""

from __future__ import annotations

import html
import json

import httpx
import pytest

from coffeedata import http
from coffeedata.adapters import fetch_catalog, generic, woocommerce
from coffeedata.http import Blocked, PoliteClient, SiteDisallowed
from coffeedata.lake import SCHEMAS
from coffeedata.models import Roaster
from coffeedata.text import html_to_text


@pytest.fixture(autouse=True)
def _no_sleep_no_egress(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "egress_ip", lambda: "203.0.113.7")


YOAST = """User-agent: *
Disallow: /wp-json/
Disallow: /?rest_route=

Sitemap: https://woo.com/sitemap_index.xml
"""


def _woo_store(robots: str):
    seen = []

    def handler(request):
        p = request.url.path
        seen.append(p)
        if p == "/robots.txt":
            return httpx.Response(200, text=robots)
        if p.startswith("/wp-json/"):
            return httpx.Response(200, json=[{"id": 7, "name": "Kenya", "prices": {"price": "1800", "currency_code": "USD",
                                                                              "currency_minor_unit": 2}}])  # fmt: skip
        if p == "/sitemap.xml":
            return httpx.Response(404)
        if p == "/sitemap_index.xml":
            return httpx.Response(
                200,
                text=(
                    "<sitemapindex><sitemap><loc>https://woo.com/product-sitemap.xml</loc></sitemap></sitemapindex>"
                ),
            )
        if p == "/product-sitemap.xml":
            return httpx.Response(
                200, text="<urlset><url><loc>https://woo.com/product/kenya/</loc></url></urlset>"
            )
        if p == "/product/kenya/":
            ld = {
                "@type": "Product",
                "name": "Kenya",
                "sku": "K1",
                "offers": {"price": "18", "priceCurrency": "USD"},
            }
            return httpx.Response(200, text=f'<script type="application/ld+json">{json.dumps(ld)}</script>')
        return httpx.Response(404)

    return handler, seen


WOO = Roaster(roaster_id="woo.com", name="W", host="woo.com", platform="woocommerce")


def test_a_store_api_disallowed_by_robots_is_read_from_the_sitemap_robots_names():
    """martinhenrycoffee.com, 2026-11-16: Yoast's `Disallow: /wp-json/` under `User-agent: *`, product pages
    allowed, its sitemap at /sitemap_index.xml. Before: `Blocked: .../wp-json/wc/store/v1/products`, 72 coffees
    lost."""
    handler, seen = _woo_store(YOAST)
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c, c.track() as stats:
        items = fetch_catalog(c, WOO)
    assert [i.title for i in items] == ["Kenya"]
    assert not any(p.startswith("/wp-json/") for p in seen)  # never asked: robots.txt is checked first
    assert stats.route.startswith("generic: robots.txt disallows the Store API")


def test_a_store_that_also_disallows_its_product_pages_stays_blocked():
    handler, seen = _woo_store(
        YOAST.replace("Disallow: /?rest_route=", "Disallow: /?rest_route=\nDisallow: /product/")
    )
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c, pytest.raises(Blocked):
        fetch_catalog(c, WOO)
    assert "/product/kenya/" not in seen and not any(p.startswith("/wp-json/") for p in seen)


def test_a_store_closed_to_us_stays_closed():
    handler, seen = _woo_store("User-agent: *\nDisallow: /\n")
    with (
        PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c,
        pytest.raises(SiteDisallowed),
    ):
        fetch_catalog(c, WOO)
    assert seen == ["/robots.txt"]


def test_the_store_api_path_is_unchanged_when_allowed():
    handler, seen = _woo_store("User-agent: *\nAllow: /\n")
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c, c.track() as stats:
        items = fetch_catalog(c, WOO)
    assert [i.title for i in items] == ["Kenya"] and stats.route is None
    assert "/sitemap.xml" not in seen and any(p.startswith("/wp-json/") for p in seen)


def test_a_block_after_the_first_page_is_not_a_reason_to_read_the_pages():
    """Review finding 4: only the first listing page's own URL, before anything was read, turns into the sitemap path.
    Here page 1 answers 100 products and page 2 redirects to a Store API path robots.txt disallows: before, the prefix
    match threw the 100 products away and read the whole store again from its pages."""
    robots = "User-agent: *\nDisallow: /wp-json/wc/store/v1/products/moved\n"
    seen = []

    def product(i):
        return {"id": i, "name": f"Coffee {i}", "prices": {"price": "1800", "currency_code": "USD",
                                                          "currency_minor_unit": 2}}  # fmt: skip

    def handler(request):
        p = request.url.path
        seen.append(p)
        if p == "/robots.txt":
            return httpx.Response(200, text=robots)
        if p == "/wp-json/wc/store/v1/products":
            if request.url.params.get("page") == "1":
                return httpx.Response(
                    200, json=[product(i) for i in range(1, 101)], headers={"x-wp-total": "150"}
                )
            return httpx.Response(301, headers={"location": "/wp-json/wc/store/v1/products/moved?page=2"})
        return httpx.Response(404)

    with (
        PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c,
        c.track() as stats,
        pytest.raises(Blocked),
    ):
        fetch_catalog(c, WOO)
    assert (
        stats.route is None
        and "/sitemap.xml" not in seen
        and "/wp-json/wc/store/v1/products/moved" not in seen
    )


def test_a_plain_generic_store_never_reads_the_sitemap_robots_names():
    """Review finding 5: only the fallback reads a robots-named sitemap; a generic store with /sitemap.xml a 404 has
    no sitemap, exactly as on main."""
    handler, seen = _woo_store(YOAST)
    r = Roaster(roaster_id="woo.com", name="W", host="woo.com", platform="other")
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c:
        assert fetch_catalog(c, r) == []
    assert "/sitemap_index.xml" not in seen


def test_the_first_declared_sitemap_that_answers_200_is_read():
    """Review finding 5: a 410 or 500 on the first declared sitemap is skipped for the next, as the docstring says."""
    robots = YOAST.replace("Sitemap: https://woo.com/sitemap_index.xml",
                           "Sitemap: https://woo.com/gone.xml\nSitemap: https://woo.com/broken.xml\n"
                           "Sitemap: https://other.com/sitemap.xml\nSitemap: https://woo.com/sitemap_index.xml")  # fmt: skip
    base, seen = _woo_store(robots)

    def handler(request):
        if request.url.path == "/gone.xml":
            seen.append("/gone.xml")
            return httpx.Response(410)
        if request.url.path == "/broken.xml":
            seen.append("/broken.xml")
            return httpx.Response(500)
        return base(request)

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, retries=1) as c:
        items = fetch_catalog(c, WOO)
    assert [i.title for i in items] == ["Kenya"]
    assert "/gone.xml" in seen and "/broken.xml" in seen and "/sitemap_index.xml" in seen
    assert all(not p.startswith("/wp-json/") for p in seen)  # and another host's sitemap is never asked


def test_the_fallback_is_recorded_in_catalog_route():
    assert "catalog_route" in SCHEMAS["fetches"].names
    assert woocommerce.STORE_API_PATH == "/wp-json/wc/store/v1/products" and callable(generic.read_pages)


# --- Identity: a product read from its page keeps the Store API's ids ------------------------------------------------
# martinhenrycoffee.com's 133 products were stored under Store API ids (product and variation post ids). Read from
# their pages instead, they keep them: the post id on the page (body class `postid-`, `<div id="product-">`, or the
# shortlink), and each size's variation id from the add-to-cart form. Invented products, as WordPress and WooCommerce
# print them.

BLEND_API = {
    "id": 247705, "name": "Heritage Blend", "slug": "heritage-blend", "type": "variable",
    "permalink": "https://woo.com/product/heritage-blend/", "is_in_stock": True,
    "short_description": "", "description": "<p>Chocolate and cherry.</p>",
    "images": [{"src": "https://woo.com/wp-content/uploads/blend.jpg"}],
    "categories": [{"name": "Coffee"}], "tags": [{"name": "Blends"}],
    "attributes": [{"name": "Grind", "terms": []}, {"name": "Size", "terms": []}],
    "variations": [{"id": 247706, "attributes": [{"name": "Grind", "value": "ground-drip"}, {"name": "Size", "value": "12oz"}]},
                   {"id": 247707, "attributes": [{"name": "Grind", "value": "whole-bean"}, {"name": "Size", "value": "12oz"}]},
                   {"id": 247708, "attributes": [{"name": "Grind", "value": None}, {"name": "Size", "value": "2lb"}]}],
    "prices": {"price": "1845", "currency_code": "USD", "currency_minor_unit": 2,
               "price_range": {"min_amount": "1845", "max_amount": "1845"}},
}  # fmt: skip
MUG_API = {
    "id": 225000, "name": "Diner Mug", "slug": "diner-mug", "type": "simple",
    "permalink": "https://woo.com/product/diner-mug/", "is_in_stock": True,
    "short_description": "", "description": "<p>Holds 12 oz.</p>",
    "images": [{"src": "https://woo.com/wp-content/uploads/mug.jpg"}],
    "categories": [{"name": "Merch"}], "tags": [],
    "prices": {"price": "1500", "currency_code": "USD", "currency_minor_unit": 2},
}  # fmt: skip


def _ld(p: dict, price: str) -> str:
    ld = {"@context": "https://schema.org/", "@graph": [{
        "@type": "Product", "@id": p["permalink"] + "#product", "name": p["name"], "url": p["permalink"],
        "description": html_to_text(p["description"]), "image": p["images"][0]["src"], "sku": "",
        "offers": [{"@type": "Offer", "price": price, "priceCurrency": "USD",
                    "availability": "http://schema.org/InStock"}]}]}  # fmt: skip
    return f'<script type="application/ld+json" class="yoast-schema-graph">{json.dumps(ld)}</script>'


def _meta(p: dict) -> str:
    cats = ", ".join(
        f'<a href="https://woo.com/c/{c["name"]}/" rel="tag">{c["name"]}</a>' for c in p["categories"]
    )
    tags = ", ".join(f'<a href="https://woo.com/t/{t["name"]}/" rel="tag">{t["name"]}</a>' for t in p["tags"])
    out = f'<div class="product_meta"><span class="posted_in">Category: {cats}</span>'
    return out + (f'<span class="tagged_as">Tag: {tags}</span>' if tags else "") + "</div>"


def _page(p: dict, ids: str = "all", inline: bool = True) -> str:
    """p's product page. ids: which of WordPress's post-id markers it carries (all, shortlink, none)."""
    pid = p["id"]
    body = (
        f'<body class="product-template-default single single-product postid-{pid} theme-x">'
        if ids == "all"
        else "<body>"
    )
    head = f'<link rel="shortlink" href="https://woo.com/?p={pid}" />' if ids in ("all", "shortlink") else ""
    div = f'<div id="product-{pid}" class="product type-product post-{pid} status-publish instock product-type-{p["type"]}">'
    if ids != "all":
        div = f'<div class="product type-product status-publish instock product-type-{p["type"]}">'
    form = ""
    if p["type"] == "variable":
        names = [a["name"] for a in p["attributes"]]
        keys = {"Grind": "attribute_pa_grind", "Size": "attribute_size"}
        data = [{"variation_id": v["id"], "display_price": 18.45, "is_in_stock": True, "variation_is_active": True,
                 "attributes": {keys[a["name"]]: a["value"] or "" for a in v["attributes"]}}
                for v in p["variations"]] if inline else False  # fmt: skip
        attr = html.escape(json.dumps(data), quote=True)
        form = f'<form class="variations_form cart" data-product_id="{pid}" data-product_variations="{attr}">'
        form += "".join(f'<select name="{keys[n]}"></select>' for n in names) + "</form>"
    price = "18.45" if p["type"] == "variable" else "15.00"
    related = '<ul class="products"><li class="product type-product post-999 product-type-simple"></li></ul>'
    return (f"<html><head>{head}{_ld(p, price)}</head>{body}{div}{form}{_meta(p)}</div>"
            f"<section class=\"related products\">{related}</section></body></html>")  # fmt: skip


def _both_ways(pages: dict[str, str]):
    """A store answering its Store API (when robots.txt allows it) and its pages."""

    def handler(request):
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text=handler.robots)
        if p == "/wp-json/wc/store/v1/products":
            return httpx.Response(200, json=[BLEND_API, MUG_API], headers={"x-wp-total": "2"})
        if p == "/sitemap.xml":
            locs = "".join(f"<url><loc>https://woo.com{u}</loc></url>" for u in pages)
            return httpx.Response(200, text=f"<urlset>{locs}</urlset>")
        if p in pages:
            return httpx.Response(200, text=pages[p])
        return httpx.Response(404)

    return handler


def _read(handler, robots: str):
    handler.robots = robots
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c:
        return {i.handle: i for i in fetch_catalog(c, WOO)}


def _shape(i):
    return (i.product_id, i.platform, i.title, i.url, i.product_type, i.tags, i.image,
            [(v.variant_id, v.title, v.price_usd, v.grams, v.available, v.currency) for v in i.variants])  # fmt: skip


def test_a_page_read_product_keeps_the_store_apis_ids():
    pages = {"/product/heritage-blend/": _page(BLEND_API), "/product/diner-mug/": _page(MUG_API)}
    handler = _both_ways(pages)
    api = _read(handler, "User-agent: *\nAllow: /\n")
    paged = _read(handler, YOAST.replace("https://woo.com/sitemap_index.xml", "https://woo.com/sitemap.xml"))
    assert api.keys() == paged.keys() == {"heritage-blend", "diner-mug"}
    for h in api:
        assert _shape(paged[h]) == _shape(api[h]), h
    assert [v.variant_id for v in paged["heritage-blend"].variants] == ["247706", "247707", "247708"]
    assert (
        paged["heritage-blend"].variants[2].title == " 2lb"
    )  # an `any` attribute is empty, as the Store API's
    assert (
        paged["diner-mug"].variants[0].variant_id == "225000"
    )  # a simple product's one size: the product's id


def test_the_shortlink_alone_gives_the_post_id():
    item = woocommerce.parse_page(_page(MUG_API, ids="shortlink"), MUG_API["permalink"], WOO)
    assert item is not None and (item.product_id, item.platform) == ("225000", "woocommerce")
    assert woocommerce.post_id('<link href="https://woo.com/?p=12" rel="shortlink">') == "12"
    assert woocommerce.post_id('<div id="product-34" class="product">') == "34"


def test_a_page_without_a_post_id_keeps_the_json_ld_ids_as_generic():
    item = woocommerce.parse_page(_page(MUG_API, ids="none"), MUG_API["permalink"], WOO)
    plain = generic.page_item(_page(MUG_API, ids="none"), MUG_API["permalink"], WOO)
    assert item == plain and item is not None and item.platform == "generic"


def test_sizes_not_inline_keep_the_json_ld_offers_under_the_post_id():
    """More variations than the store shows inline (data-product_variations="false"): the product keeps its id, and
    its sizes are what the JSON-LD offers say (their ids differ from the Store API's)."""
    item = woocommerce.parse_page(_page(BLEND_API, inline=False), BLEND_API["permalink"], WOO)
    assert item is not None and item.product_id == "247705" and item.platform == "woocommerce"
    assert [v.variant_id for v in item.variants] == ["0"]
