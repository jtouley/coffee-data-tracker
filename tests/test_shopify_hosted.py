"""Shopify-hosted stores the generic crawl read page by page (group B of the 2026-11-16 crawl recovery, split from
#178): `shopify?` storefronts are crawled by the Shopify job; a generic store whose sitemap index is Shopify's reads
/products.json under its own budget.

No test sends a request anywhere: every response comes from httpx.MockTransport.
"""

from __future__ import annotations

import json

import httpx
import pytest

from coffeedata import discover, http
from coffeedata.adapters import fetch_catalog, generic
from coffeedata.http import PoliteClient
from coffeedata.models import Roaster
from coffeedata.ratelimit import SHOPIFY_HOSTED, PlatformLimiter


@pytest.fixture(autouse=True)
def _no_sleep_no_egress(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "egress_ip", lambda: "203.0.113.7")  # never ask the echo service


# --- B: Shopify storefronts the generic crawl reads ------------------------------------------------------------------


def _shopify_store(products_json_status=200, challenge=False):
    seen = []

    def handler(request):
        p = request.url.path
        seen.append(p)
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /cart\n")
        if p == "/products.json":
            if challenge:
                return httpx.Response(429, headers={"cf-mitigated": "challenge"}, text="<html>")
            if products_json_status != 200:
                return httpx.Response(products_json_status, text="nope")
            page = int(request.url.params.get("page", "1"))
            prods = [] if page > 1 else [{"id": 1, "title": "Ethiopia", "handle": "ethiopia",
                                          "variants": [{"id": 11, "title": "12 oz", "price": "20.00"}]}]  # fmt: skip
            return httpx.Response(200, json={"products": prods})
        if p == "/sitemap.xml":
            return httpx.Response(200, text=(
                "<sitemapindex><sitemap><loc>https://shop.com/sitemap_products_1.xml?from=1&amp;to=9</loc></sitemap>"
                "</sitemapindex>"))  # fmt: skip
        if p.startswith("/sitemap_products_1.xml"):
            return httpx.Response(
                200, text="<urlset><url><loc>https://shop.com/products/ethiopia</loc></url></urlset>"
            )
        if p == "/products/ethiopia":
            ld = {
                "@type": "Product",
                "name": "Ethiopia",
                "sku": "E1",
                "offers": {"price": "20", "priceCurrency": "USD"},
            }
            return httpx.Response(200, text=f'<script type="application/ld+json">{json.dumps(ld)}</script>')
        return httpx.Response(404)

    return handler, seen


def _limited_client(handler, name):
    lim = PlatformLimiter(name, rate=1000.0, max_concurrent=4, sleep=lambda s: None)
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, limiters={name: lim}), lim


def test_a_shopify_marked_store_is_crawled_by_the_shopify_job():
    """`shopify?` (the probe saw cdn.shopify.com, but /products.json didn't answer it): before, the generic job read
    it page by page from the WooCommerce/generic IP, where 19 such stores drew Cloudflare's challenge on 2026-11-16.
    Now the Shopify adapter, so the crawl-shopify job (connectors filter by adapter), its IP and its budget."""
    assert Roaster(roaster_id="shop.com", name="S", host="shop.com", platform="shopify?").adapter == "shopify"
    assert Roaster(roaster_id="x.com", name="X", host="x.com", platform="other").adapter == "generic"


def test_a_shopify_marked_store_is_read_from_products_json_under_shopifys_budget():
    handler, seen = _shopify_store()
    client, lim = _limited_client(handler, "shopify")
    r = Roaster(roaster_id="shop.com", name="S", host="shop.com", platform="shopify?")
    with client, client.track() as stats:
        items = fetch_catalog(client, r)
    assert [i.title for i in items] == ["Ethiopia"] and items[0].platform == "shopify"
    assert "/products/ethiopia" not in seen and "/sitemap.xml" not in seen
    assert stats.route is not None and stats.route.startswith("shopify /products.json")
    assert lim.stats["requests"] == len(seen)


def test_a_marked_store_without_products_json_is_read_from_its_pages_on_the_same_budget():
    handler, seen = _shopify_store(products_json_status=404)
    client, lim = _limited_client(handler, "shopify")
    r = Roaster(roaster_id="shop.com", name="S", host="shop.com", platform="shopify?")
    with client, client.track() as stats:
        items = fetch_catalog(client, r)
    assert [i.title for i in items] == ["Ethiopia"] and items[0].platform == "generic"
    assert "/products/ethiopia" in seen and stats.route is not None
    assert stats.route.startswith("pages: /products.json not served")
    assert lim.stats["requests"] == len(seen)  # every request, robots.txt included, on Shopify's budget


def test_a_challenge_on_products_json_is_the_answer_never_a_reason_to_read_pages():
    handler, seen = _shopify_store(challenge=True)
    client, _ = _limited_client(handler, "shopify")
    r = Roaster(roaster_id="shop.com", name="S", host="shop.com", platform="shopify?")
    with client, pytest.raises(http.FetchError) as e:
        fetch_catalog(client, r)
    assert http.challenged(e.value) and "/products/ethiopia" not in seen and "/sitemap.xml" not in seen


def test_a_generic_store_whose_sitemap_is_shopifys_reads_products_json_under_the_hosted_budget():
    """Not yet re-probed: the generic job finds Shopify's sitemap index and reads /products.json (a few requests
    instead of one per product), at the Shopify-hosted budget (1 request/s, 4 at once, across all of them)."""
    handler, seen = _shopify_store()
    client, lim = _limited_client(handler, SHOPIFY_HOSTED)
    r = Roaster(roaster_id="shop.com", name="S", host="shop.com", platform="other")
    with client, client.track() as stats:
        items = fetch_catalog(client, r)
    assert [i.title for i in items] == ["Ethiopia"] and "/products/ethiopia" not in seen
    assert stats.route == "shopify /products.json: its sitemap index is Shopify's"
    assert lim.stats["requests"] >= 1


def test_the_probe_marks_a_store_with_shopifys_sitemap_index():
    handler, _ = _shopify_store()

    def probing(request):
        if request.url.path == "/":
            return httpx.Response(200, text="<html>no markers</html>")
        if request.url.path == "/products.json":
            return httpx.Response(403, text="no")  # didn't answer the probe
        return handler(request)

    with PoliteClient(transport=httpx.MockTransport(probing), min_interval=0) as c:
        res = discover.probe_one(c, "shop.com")
    assert res["status"] == "ok" and res["platform"] == "shopify?"


def test_generic_shopify_sitemap_names():
    assert generic.shopify_sitemap(["https://x.com/sitemap_products_1.xml?from=1&to=2"])
    assert not generic.shopify_sitemap(["https://x.com/product-sitemap.xml"])
