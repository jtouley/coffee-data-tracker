"""A WooCommerce store whose robots.txt disallows the Store API (martinhenrycoffee.com, run 2026-11-16: Yoast's
`Disallow: /wp-json/` and `Disallow: /?rest_route=` under `User-agent: *`) is read from its sitemap (or the one its
robots.txt names) and product pages, under the same rules and spacing. Split from #178: it changes these products'
ids (CLAUDE.md step 9). No test sends a request anywhere: every response comes from an httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from coffeedata import http
from coffeedata.adapters import fetch_catalog, generic, woocommerce
from coffeedata.http import Blocked, PoliteClient, SiteDisallowed
from coffeedata.lake import SCHEMAS
from coffeedata.models import Roaster


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
                return httpx.Response(200, json=[product(i) for i in range(1, 101)], headers={"x-wp-total": "150"})
            return httpx.Response(301, headers={"location": "/wp-json/wc/store/v1/products/moved?page=2"})
        return httpx.Response(404)

    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c, c.track() as stats:
        with pytest.raises(Blocked):
            fetch_catalog(c, WOO)
    assert stats.route is None and "/sitemap.xml" not in seen and "/wp-json/wc/store/v1/products/moved" not in seen


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
