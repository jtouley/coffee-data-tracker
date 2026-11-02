"""No cap on how much of a catalog a fetch reads (the owner's decision, 2026-11-02), WooCommerce's variation prices in
bulk, and a fetch the run's deadline stops mid-catalog stored as `partial`, never as the whole catalog."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from coffeedata import http, ingest, sitemap
from coffeedata.adapters import generic, shopify, square_online, squarespace, woocommerce
from coffeedata.http import DeadlineReached, FetchError, PartialCatalog, PoliteClient
from coffeedata.models import CatalogItem, Roaster

WEEK = "2026-11-02"
BASE = "https://w.com/wp-json/wc/store/v1/products"


def _r(host: str, platform: str) -> Roaster:
    return Roaster(roaster_id=host, name=host, host=host, platform=platform)


class Clock:
    """http's clock: a handler moves it past the client's deadline mid-catalog."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(http, "time", c)
    return c


class Site:
    """A MockTransport that records every request (robots.txt aside) and answers with `handler`."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        self.requests.append(req)
        return self.handler(req)

    def client(self, **kw) -> PoliteClient:
        return PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(self), **kw)


# --- WooCommerce: the Store API, variation prices in bulk ---------------------------------------------------


def _woo_store(n_products: int, sizes: int, ignore_type: bool = False, bulk_status: int = 200):
    """A Store API: n_products variable products of `sizes` sizes each, priced 10 + size (minor units x100). It
    answers type=variation&parent=... with those parents' variations (ignore_type: it ignores type and parent, as an
    older Store API might), and /products/<variation id> with that one."""
    products, variations = [], []
    for i in range(n_products):
        pid = 10_000 + i
        vs = [
            {"id": pid * 1000 + s, "attributes": [{"name": "Size", "value": f"{s + 1} lb"}]}
            for s in range(sizes)
        ]
        products.append({"id": pid, "name": f"Coffee {i}", "slug": f"c{i}", "variations": vs,
                         "prices": {"price": "1000", "currency_minor_unit": 2,
                                    "price_range": {"min_amount": "1000", "max_amount": "9000"}}})  # fmt: skip
        variations += [{"id": v["id"], "parent": pid, "prices": {"price": str(1000 + 100 * s),
                        "currency_minor_unit": 2}} for s, v in enumerate(vs)]  # fmt: skip
    by_id = {v["id"]: v for v in variations}

    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        tail = req.url.path.removeprefix("/wp-json/wc/store/v1/products").strip("/")
        if tail:
            return httpx.Response(200, json=by_id[int(tail)])
        per_page, page = int(q.get("per_page", 10)), int(q.get("page", 1))
        rows = products
        if q.get("type") == "variation" and not ignore_type:
            if bulk_status != 200:
                return httpx.Response(bulk_status, text="<html>sg-captcha</html>")
            parents = {int(x) for x in q["parent"].split(",")}
            rows = [v for v in variations if v["parent"] in parents]
        return httpx.Response(200, json=rows[(page - 1) * per_page : page * per_page])

    return handler, variations


def _prices(items: list[CatalogItem]) -> dict[str, float | None]:
    return {v.variant_id: v.price_usd for i in items for v in i.variants}


def test_woocommerce_prices_every_size_in_bulk_with_no_lookup_cap():
    """hoodrivercoffeeroasters.com's case at scale: 60 products of 10 sizes is 600 variations, four times the old
    MAX_VARIATION_LOOKUPS. Every one is priced, from about one request per 100 variations, none on its own."""
    assert not hasattr(woocommerce, "MAX_VARIATION_LOOKUPS") and not hasattr(woocommerce, "MAX_PAGES")
    handler, variations = _woo_store(60, 10)
    site = Site(handler)
    with site.client() as c:
        items = woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert len(items) == 60
    assert _prices(items) == {str(v["id"]): int(v["prices"]["price"]) / 100 for v in variations}
    bulk = [r for r in site.requests if r.url.params.get("type") == "variation"]
    assert all(r.url.path.endswith("/products") for r in site.requests)  # no per-variation request
    assert len(site.requests) == 1 + len(bulk) and len(bulk) == 6  # one listing page, 600 / 100 bulk pages


def test_woocommerce_bulk_batches_parents_and_pages_a_batch_over_100():
    """A product with more sizes than a page holds is paged within its batch; parents are sent comma-separated."""
    handler, variations = _woo_store(3, 130)
    site = Site(handler)
    with site.client() as c:
        items = woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert all(p is not None for p in _prices(items).values()) and len(_prices(items)) == 390
    bulk = [r for r in site.requests if r.url.params.get("type") == "variation"]
    assert [r.url.params["parent"] for r in bulk] == ["10000"] * 2 + ["10001"] * 2 + ["10002"] * 2


def test_woocommerce_reads_past_20_listing_pages():
    """villageofartisans.com stopped at 2,000 products (MAX_PAGES = 20 of 100) with no error: now every page."""
    products = [
        {"id": i, "name": f"C{i}", "slug": f"c{i}", "prices": {"price": "1800"}} for i in range(2_550)
    ]

    def handler(req):
        per_page, page = int(req.url.params["per_page"]), int(req.url.params["page"])
        return httpx.Response(200, json=products[(page - 1) * per_page : page * per_page])

    site = Site(handler)
    with site.client() as c:
        assert len(woocommerce.fetch(c, _r("w.com", "woocommerce"))) == 2_550
    assert len(site.requests) == 26


def test_woocommerce_store_that_ignores_type_variation_falls_back_to_lookups_with_no_cap():
    """An older Store API answers type=variation with its products: each size is asked for on its own, as before,
    and all 200 of them (the old cap stopped at 150). Each batch notices the ignored filter on its first page (a
    variation's `parent` outside the batch), so it costs one bulk request per batch of about 100 sizes."""
    handler, variations = _woo_store(20, 10, ignore_type=True)
    site = Site(handler)
    with site.client() as c:
        items = woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert _prices(items) == {str(v["id"]): int(v["prices"]["price"]) / 100 for v in variations}
    lookups = [r for r in site.requests if r.url.path.rstrip("/").split("/")[-1].isdigit()]
    assert len(lookups) == 200
    assert sum(r.url.params.get("type") == "variation" for r in site.requests) == 2  # 2 batches of 100 sizes


def test_woocommerce_failed_bulk_page_fails_the_fetch_instead_of_wiping_prices():
    """mozzaroasters.com: SiteGround answered 202 to the price lookups and 24 sizes lost their prices in an ok fetch.
    A bulk page that fails now fails the fetch (retried), like a listing page."""
    handler, _ = _woo_store(3, 8, bulk_status=202)
    with Site(handler).client() as c, pytest.raises(FetchError, match="HTTP 202"):
        woocommerce.fetch(c, _r("w.com", "woocommerce"))


def test_woocommerce_store_that_repeats_a_listing_page_ends():
    """A store that ignores `page` would be read forever with no page cap: a page with nothing new ends it."""
    full = [{"id": i, "name": f"C{i}", "slug": f"c{i}", "prices": {"price": "1800"}} for i in range(100)]
    site = Site(lambda req: httpx.Response(200, json=full))
    with site.client() as c:
        assert len(woocommerce.fetch(c, _r("w.com", "woocommerce"))) == 100
    assert len(site.requests) == 2


def test_woocommerce_deadline_mid_prices_keeps_only_products_read_whole(clock):
    """The deadline passes after the first bulk page: the products it priced, and the simple ones, are the partial
    catalog; a product whose sizes weren't all priced is left out (left as it was), never stored unpriced."""
    handler, _ = _woo_store(4, 50)  # two products per bulk batch
    simple = {
        "id": 1,
        "name": "Drip bags",
        "slug": "d",
        "prices": {"price": "1200", "currency_minor_unit": 2},
    }

    def h(req):
        if req.url.params.get("type") == "variation":
            clock.t += 10_000  # the job's window ends while this page is read
            return handler(req)
        r = handler(req)
        return httpx.Response(200, json=[simple, *r.json()]) if req.url.params.get("page") == "1" else r

    with Site(h).client(deadline_s=60) as c, pytest.raises(PartialCatalog) as e:
        woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert sorted(i.product_id for i in e.value.items) == ["1", "10000", "10001"]
    assert all(v.price_usd is not None for i in e.value.items for v in i.variants)
    assert isinstance(e.value.__cause__, DeadlineReached)


# --- the other adapters' caps ------------------------------------------------------------------------------


def _urlset(urls) -> str:
    return "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"


def _product_page(i: int) -> str:
    ld = f'{{"@type": "Product", "name": "Coffee {i}", "sku": "s{i}", "offers": {{"price": "18"}}}}'
    return f'<script type="application/ld+json">{ld}</script>'


def test_generic_reads_every_product_page_past_80():
    """morghanrakecoffee.com read a different 80 of its 118 products each week, unmarked."""
    assert not hasattr(generic, "MAX_PAGES")
    urls = [f"https://g.com/products/c{i}" for i in range(130)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(urls))
        return httpx.Response(200, text=_product_page(int(req.url.path.rsplit("c", 1)[1])))

    with Site(handler).client() as c:
        assert len(generic.fetch(c, _r("g.com", "custom"))) == 130


def test_generic_finds_products_after_ten_pages_without_markup():
    urls = [f"https://g.com/products/c{i}" for i in range(15)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(urls))
        i = int(req.url.path.rsplit("c", 1)[1])
        return httpx.Response(200, text=_product_page(i) if i >= 12 else "<p>no markup</p>")

    with Site(handler).client() as c:
        assert sorted(i.product_id for i in generic.fetch(c, _r("g.com", "custom"))) == ["s12", "s13", "s14"]


def test_generic_deadline_stores_the_pages_read_as_partial(clock):
    urls = [f"https://g.com/products/c{i}" for i in range(10)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(urls))
        i = int(req.url.path.rsplit("c", 1)[1])
        if i == 3:
            clock.t += 10_000
        return httpx.Response(200, text=_product_page(i))

    with Site(handler).client(deadline_s=60) as c, pytest.raises(PartialCatalog) as e:
        generic.fetch(c, _r("g.com", "custom"))
    assert sorted(i.product_id for i in e.value.items) == ["s0", "s1", "s2", "s3"]


def test_sitemap_index_reads_every_sub_sitemap():
    """The old index read stopped at 5 sub-sitemaps (and 5,000 URLs)."""
    subs = [f"https://g.com/sitemap-{n}.xml" for n in range(8)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text="<sitemapindex>" + "".join(f"<sitemap><loc>{s}</loc></sitemap>" for s in subs) + "</sitemapindex>")  # fmt: skip
        n = int(req.url.path.split("-")[1].split(".")[0])
        return httpx.Response(200, text=_urlset(f"https://g.com/products/s{n}-{i}" for i in range(1_000)))

    site = Site(handler)
    with site.client() as c:
        assert len(sitemap.sitemap_urls(c, "g.com")) == 8_000
    assert len(site.requests) == 9


def test_shopify_deadline_keeps_the_pages_read_as_partial(clock):
    def handler(req):
        page = int(req.url.params["page"])
        if page == 3:
            clock.t += 10_000
        return httpx.Response(200, json={"products": [{"id": page * 1000 + i, "title": "t", "handle": f"h{i}",
                                                       "variants": []} for i in range(250)]})  # fmt: skip

    with Site(handler).client(deadline_s=60) as c, pytest.raises(PartialCatalog) as e:
        shopify.fetch(c, _r("s.com", "shopify"))
    assert len(e.value.items) == 3 * 250


def _sq_store(pages: int, clock: Clock | None = None, stop_at: int | None = None):
    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(404)
        if req.url.path != "/shop":
            return httpx.Response(404)
        page = int(req.url.params.get("offset", "0"))
        if clock is not None and page == stop_at:
            clock.t += 10_000
        items = [{"id": f"{page}-{i}", "title": "t", "variants": [{"id": f"v{page}-{i}"}]} for i in range(20)]
        more = page + 1 < pages
        return httpx.Response(200, json={"items": items, "pagination": {"nextPage": more, "nextPageOffset": str(page + 1)}})  # fmt: skip

    return handler


def test_squarespace_reads_every_collection_page_past_10():
    assert not hasattr(squarespace, "MAX_PAGES")
    with Site(_sq_store(15)).client() as c:
        assert len(squarespace.fetch(c, _r("q.com", "squarespace"))) == 15 * 20


def test_squarespace_tries_every_shop_path():
    """The old fetch tried 6 paths, from the 4 busiest sitemap parents: a shop under the 7th was never found."""
    urls = [
        f"https://q.com/{d}/x{i}"
        for d in ("blog", "news", "events", "recipes", "about", "team")
        for i in range(9)
    ]
    urls += [f"https://q.com/beans-shop/x{i}" for i in range(3)]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(urls))
        if req.url.path == "/beans-shop":
            return httpx.Response(200, json={"items": [{"id": "1", "title": "t", "variants": [{"id": "v"}]}]})
        return httpx.Response(404)

    assert squarespace.shop_paths(urls).index("/beans-shop") >= 6
    with Site(handler).client() as c:
        assert len(squarespace.fetch(c, _r("q.com", "squarespace"))) == 1


def test_squarespace_repeated_offset_ends_the_collection():
    def handler(req):
        if req.url.path != "/shop":
            return httpx.Response(404)
        return httpx.Response(200, json={"items": [{"id": "1", "title": "t", "variants": [{"id": "v"}]}],
                                         "pagination": {"nextPage": True, "nextPageOffset": "7"}})  # fmt: skip

    site = Site(handler)
    with site.client() as c:
        assert len(squarespace.fetch(c, _r("q.com", "squarespace"))) == 1
    assert sum(r.url.path == "/shop" for r in site.requests) == 2


def test_squarespace_deadline_keeps_the_pages_read_as_partial(clock):
    with Site(_sq_store(5, clock, stop_at=2)).client(deadline_s=60) as c, pytest.raises(PartialCatalog) as e:
        squarespace.fetch(c, _r("q.com", "squarespace"))
    assert len(e.value.items) == 3 * 20


def _square_store(pages: int, clock: Clock | None = None, stop_at: int | None = None):
    def handler(req):
        if req.url.path == "/":
            return httpx.Response(200, text='{"user":{"id":143063903},"site_id":"480502767511180931"}')
        page = int(req.url.params["page"])
        if clock is not None and page == stop_at:
            clock.t += 10_000
        data = [
            {"id": f"{page}-{i}", "name": "n", "price": {"regular_low": 18, "regular_high": 18}}
            for i in range(60)
        ]
        return httpx.Response(200, json={"data": data, "meta": {"pagination": {"total_pages": pages}}})

    return handler


def test_square_online_reads_every_page_past_20():
    assert not hasattr(square_online, "MAX_PAGES")
    with Site(_square_store(25)).client() as c:
        assert len(square_online.fetch(c, _r("sq.com", "square_online"))) == 25 * 60


def test_square_online_deadline_keeps_the_pages_read_as_partial(clock):
    with (
        Site(_square_store(5, clock, stop_at=4)).client(deadline_s=60) as c,
        pytest.raises(PartialCatalog) as e,
    ):
        square_online.fetch(c, _r("sq.com", "square_online"))
    assert len(e.value.items) == 4 * 60


# --- the deadline: no request after it; a stopped catalog is stored partial -------------------------------


def test_no_request_is_sent_after_the_clients_deadline(clock):
    site = Site(lambda req: httpx.Response(200, json=[]))
    with site.client(deadline_s=60) as c:
        c.get("https://a.com/x")
        clock.t += 61
        with pytest.raises(DeadlineReached):
            c.get("https://a.com/y")
    assert [r.url.path for r in site.requests] == ["/x"]


def _fetches(lake) -> list[tuple]:
    q = f"select roaster_id, status, partial, products, error from {lake.scan('fetches')} order by roaster_id"
    return lake.connect().execute(q).fetchall()


def test_ingest_stores_a_catalog_stopped_at_the_deadline_as_partial_and_not_done(lake, clock):
    """A Shopify catalog still being read at the run's deadline: its pages so far are an ok fetch marked partial
    (dbt then keeps its other products listed), the run counts it, and a resume pass reads it again (not done). A
    store stopped before any product is an error, never an empty catalog."""

    def handler(req):
        if req.url.host == "empty.com":
            clock.t += 10_000
            return httpx.Response(200, json={"products": []})
        page = int(req.url.params["page"])
        if page == 3:
            clock.t += 10_000
        return httpx.Response(200, json={"products": [{"id": page * 1000 + i, "title": "t", "handle": f"h{i}",
                                                       "variants": []} for i in range(250)]})  # fmt: skip

    stores = [_r("big.com", "shopify")]
    out = ingest.run(lake, stores, WEEK, workers=1, deadline_s=60,
                     client_kw={"min_interval": 0, "retries": 1, "transport": httpx.MockTransport(Site(handler))})  # fmt: skip
    assert out["partial"] == 1 and out["by_adapter"]["shopify"]["partial"] == 1
    # the partial row says why it stopped, and where (fetches.error: item 6)
    assert _fetches(lake) == [
        (
            "big.com",
            "ok",
            True,
            750,
            "partial: the run's deadline passed before https://big.com/products.json (page 4)",
        )
    ]
    done, tried = ingest._done_and_tried(lake, WEEK)
    assert "big.com" not in done and "big.com" in tried
    assert ingest.known_products(lake) == {}  # a partial read is never the store's known size


def test_ingest_partial_with_nothing_read_is_an_error(lake, monkeypatch):
    r = _r("a.com", "shopify")

    def fetch(client, roaster, *a, **k):
        raise PartialCatalog("stopped", []) from DeadlineReached("the run's deadline passed")

    monkeypatch.setattr(ingest, "fetch_catalog", fetch)
    res = ingest.ingest_one(PoliteClient(), r)
    assert (res.status, res.partial, res.items) == ("error", False, [])
    assert res.error.startswith("DeadlineReached: ")


def test_rows_mark_partial_only_on_ok_fetches():
    r = _r("a.com", "shopify")
    item = CatalogItem(roaster_id="a.com", product_id="1", handle="h", url="u", title="t", platform="shopify")
    now = ingest.datetime(2026, 11, 2, tzinfo=ingest.UTC)
    results = [ingest.Result(r, now, items=[item], partial=True), ingest.Result(r, now, items=[item]),
               ingest.Result(r, now, error="boom")]  # fmt: skip
    rows = ingest.rows_for(results, date(2026, 11, 2), "b", set())["fetches"]
    assert [(x["status"], x["partial"], x["products"]) for x in rows] == [
        ("ok", True, 1), ("ok", False, 1), ("error", None, 0)
    ]  # fmt: skip
