"""Shopify-hosted stores the generic crawl read page by page (group B of the 2026-11-16 crawl recovery, split from
#178), and the identity they keep when they are read from /products.json instead.

- `shopify?` storefronts are crawled by the Shopify job; a generic store whose sitemap index is Shopify's reads
  /products.json under its own budget.
- Moving a store from its pages' JSON-LD to /products.json would give its products Shopify's numeric ids. In the lake
  copy (run 2026-11-16) the generic adapter stored the 42 `shopify?` stores' 681 products under a hash of the page URL
  (405), a JSON-LD sku or productID (273), and Shopify's numeric id in only 3. So each product keeps the ids generic
  stored it under (ingest.legacy_identity, by handle), and its variants the ids generic's rule gives them
  (shopify.generic_variant_ids): coffee_id and every price series continue (CLAUDE.md step 9).

No test sends a request anywhere: every response comes from httpx.MockTransport or respx.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from coffeedata import discover, http, ingest
from coffeedata.adapters import fetch_catalog, generic, shopify
from coffeedata.http import PoliteClient
from coffeedata.lake import Lake
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


# --- Identity: a moved product keeps the ids generic stored it under -------------------------------------------------

HOST = "moved.com"
GENERIC = Roaster(roaster_id=HOST, name="Moved", host=HOST, platform="other")
MARKED = Roaster(roaster_id=HOST, name="Moved", host=HOST, platform="shopify?")

# One store, as both adapters see it. The pages' JSON-LD is Shopify's own (one Offer per variant, in the variants'
# order, each with the variant's sku), as its themes print it; /products.json is the same catalog. Invented products.
PRODUCTS = [
    {   # JSON-LD with no product-level id: generic stored it under a hash of its URL
        "id": 7100000000001, "title": "Ethiopia Guji", "handle": "ethiopia-guji",
        "variants": [{"id": 4100000000011, "title": "12 oz", "sku": "ETH-12", "price": "20.00", "available": True},
                     {"id": 4100000000012, "title": "2 lb", "sku": "ETH-2LB", "price": "48.00", "available": True}],
    },
    {   # a product-level sku; offers without one (variant ids were positions); a percent-encoded URL
        "id": 7100000000002, "title": "Pods®", "handle": "pods®",
        "variants": [{"id": 4100000000021, "title": "Box of 12", "sku": "POD-12", "price": "14.00", "available": True},
                     {"id": 4100000000022, "title": "Box of 24", "sku": "POD-24", "price": "26.00", "available": False}],
    },
    {   # a repeated sku: generic's `#<position>`
        "id": 7100000000003, "title": "House Blend", "handle": "house-blend",
        "variants": [{"id": 4100000000031, "title": "12 oz / Whole", "sku": "HB", "price": "17.00", "available": True},
                     {"id": 4100000000032, "title": "12 oz / Ground", "sku": "HB", "price": "17.00", "available": True},
                     {"id": 4100000000033, "title": "5 lb", "sku": "", "price": "70.00", "available": True}],
    },
]  # fmt: skip
NEW = {
    "id": 7100000000009, "title": "Kenya Nyeri", "handle": "kenya-nyeri",
    "variants": [{"id": 4100000000091, "title": "12 oz", "sku": "KEN-12", "price": "23.00", "available": True}],
}  # fmt: skip


def _page(p: dict) -> str:
    """A Shopify product page's JSON-LD for product p."""
    offers = []
    for v in p["variants"]:
        o = {"@type": "Offer", "price": v["price"], "priceCurrency": "USD",
             "availability": "http://schema.org/" + ("InStock" if v["available"] else "OutOfStock")}  # fmt: skip
        if p["handle"] != "pods®":  # this theme leaves the offers' sku out
            o["sku"] = v["sku"]
        offers.append(o)
    ld = {"@context": "http://schema.org/", "@type": "Product", "name": p["title"], "offers": offers}
    if p["handle"] == "pods®":
        ld["sku"] = "POD"
    return f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'


def _url(handle: str) -> str:
    return f"https://{HOST}/products/" + ("pods%C2%AE" if handle == "pods®" else handle)


def _store(mock: respx.MockRouter, catalog: list[dict]) -> None:
    mock.get(f"https://{HOST}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    locs = "".join(f"<url><loc>{_url(p['handle'])}</loc></url>" for p in catalog)
    mock.get(f"https://{HOST}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=f"<urlset>{locs}</urlset>")
    )
    for p in catalog:
        mock.get(_url(p["handle"])).mock(return_value=httpx.Response(200, text=_page(p)))

    def products_json(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"products": catalog if page == 1 else []})

    mock.get(f"https://{HOST}/products.json").mock(side_effect=products_json)


def _stored(lake: Lake, run_date: str) -> dict[str, tuple[str, str, list[str]]]:
    """handle -> (platform, product_id, variant ids) as the run stored them."""
    rows = (
        lake.connect()
        .execute(
            f"""select handle, platform, product_id, [v.variant_id for v in variants]
            from {lake.scan("snapshots")} where cast(run_date as varchar) = ?""",
            [run_date],
        )
        .fetchall()
    )
    return {h: (plat, pid, vids) for h, plat, pid, vids in rows}


def test_a_moved_store_keeps_every_product_and_variant_id(lake):
    """Week 1 the generic adapter reads the pages; week 2 the store is `shopify?` and the Shopify job reads
    /products.json. Every product generic stored keeps its product id (a URL hash, a sku), and every variant its
    id (sku, `#<position>` for a repeat, the position where the offers had no sku): the same coffee_id and the same
    price series. A product first seen after the move takes Shopify's ids. Week 3 gives the same ids again."""
    with respx.mock(assert_all_called=False) as mock:
        _store(mock, PRODUCTS)
        ingest.run(lake, [GENERIC], "2026-11-09", workers=1)
    before = _stored(lake, "2026-11-09")
    assert {plat for plat, _, _ in before.values()} == {"generic"} and len(before) == 3
    with respx.mock(assert_all_called=False) as mock:
        _store(mock, [*PRODUCTS, NEW])
        ingest.run(lake, [MARKED], "2026-11-16", workers=1)
    after = _stored(lake, "2026-11-16")
    assert {plat for plat, _, _ in after.values()} == {"shopify"}
    # the generic handle is the URL's (percent-encoded), Shopify's is decoded: the same product
    pairs = {"ethiopia-guji": "ethiopia-guji", "pods%C2%AE": "pods®", "house-blend": "house-blend"}
    for old, new in pairs.items():
        assert after[new][1:] == before[old][1:], new
    assert before["ethiopia-guji"][2] == ["ETH-12", "ETH-2LB"]
    assert before["pods%C2%AE"][1:] == ("POD", ["0", "1"])
    assert before["house-blend"][2] == ["HB", "HB#1", "2"]
    assert after["kenya-nyeri"][1:] == (
        "7100000000009",
        ["4100000000091"],
    )  # new after the move: Shopify's ids
    with respx.mock(assert_all_called=False) as mock:
        _store(mock, [*PRODUCTS, NEW])
        ingest.run(lake, [MARKED], "2026-11-23", workers=1)
    assert _stored(lake, "2026-11-23") == after


def test_a_store_the_registry_knows_as_shopify_keeps_shopifys_ids(lake):
    """Only a store that can have been read page by page (not `shopify` in the registry) gets its old ids: a
    Shopify store's own ids never change."""
    with respx.mock(assert_all_called=False) as mock:
        _store(mock, PRODUCTS)
        ingest.run(lake, [GENERIC], "2026-11-09", workers=1)
        shop = Roaster(roaster_id=HOST, name="Moved", host=HOST, platform="shopify")
        ingest.run(lake, [shop], "2026-11-16", workers=1)
    assert _stored(lake, "2026-11-16")["ethiopia-guji"][1:] == (
        "7100000000001",
        ["4100000000011", "4100000000012"],
    )


def _snap(rid, handle, pid, vids, platform="generic", at="2026-11-02T03:00:00"):
    return {
        "run_date": datetime.fromisoformat(at).date(), "batch_id": "b" + at[:10], "roaster_id": rid,
        "product_id": pid, "handle": handle, "url": f"https://{rid}/products/{handle}", "title": handle,
        "platform": platform, "fetched_at": datetime.fromisoformat(at).replace(tzinfo=UTC),
        "variants": [{"variant_id": v, "title": "", "price_usd": 1.0, "grams": None, "available": True,
                      "currency": "USD"} for v in vids],
    }  # fmt: skip


def test_legacy_identity_reads_generic_rows_only_and_gives_each_id_to_one_handle(lake):
    rows = [
        _snap("a.com", "guji", "h1", ["0", "1"], at="2026-11-02T03:00:00"),
        _snap(
            "a.com", "guji", "h2", ["S1"], at="2026-11-09T03:00:00"
        ),  # the same handle, a later id: the later
        _snap("a.com", "old-name", "h3", ["0"], at="2026-11-02T03:00:00"),
        _snap(
            "a.com", "new-name", "h3", ["0"], at="2026-11-09T03:00:00"
        ),  # an id under a new handle: the newer
        _snap("a.com", "kenya", "999", ["9"], platform="shopify", at="2026-11-16T03:00:00"),  # not generic's
        _snap("b.com", "guji", "x1", ["0"]),  # another store
    ]
    for at in sorted({r["fetched_at"] for r in rows}):
        part = [r for r in rows if r["fetched_at"] == at]
        lake.write_partition("snapshots", str(part[0]["run_date"]), part[0]["batch_id"], part)
    ids = ingest.legacy_identity(lake, {"a.com"})
    assert ids == {
        "a.com": {
            "guji": shopify.Legacy("h2", False),
            "new-name": shopify.Legacy("h3", True),
        }
    }
    assert ingest.legacy_identity(lake, set()) == {}


def test_generic_variant_ids_follow_generic_parse_jsonld():
    """The rule generic.parse_jsonld gives an offer: its sku, else its position; a repeat gets `#<position>`."""
    vs = [{"sku": "A"}, {"sku": "A"}, {"sku": ""}, {}, {"sku": "3"}]
    assert shopify.generic_variant_ids(vs) == ["A", "A#1", "2", "3", "3#4"]
    assert shopify.generic_variant_ids(vs, positional=True) == ["0", "1", "2", "3", "4"]
    p = {"@type": "Product", "name": "X", "offers": [{"sku": "A", "price": "1"}, {"sku": "A", "price": "1"},
                                                      {"price": "1"}]}  # fmt: skip
    item = generic.parse_jsonld(p, "https://x.com/products/x", GENERIC)
    assert [v.variant_id for v in item.variants] == shopify.generic_variant_ids(vs[:3])


def test_identity_key_matches_both_adapters_handles():
    assert shopify.identity_key("k-cups%C2%AE") == shopify.identity_key("k-cups®") == "k-cups®"
    assert shopify.identity_key("Guji") == "guji"
