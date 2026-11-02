"""PR #99's fix round, one test (or more) per item of the queue. The owner's principle (2026-11-02): ingestion only
brings in data, and never stores a null for a value the store has; a lookup that fails fails (or defers) the fetch."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from coffeedata import discover, http, ingest, registry, sitemap
from coffeedata.adapters import generic, shopify, square_online, squarespace, woocommerce
from coffeedata.http import FetchError, PartialCatalog, PoliteClient, block_status, challenged
from coffeedata.ratelimit import HostRateLimited, PlatformLimiter
from tests.lakehelp import product, run
from tests.test_crawl_caps import BASE, Site, _r, _urlset, _woo_store
from tests.test_crawl_caps import clock as clock  # noqa: F401 - the fixture
from tests.test_ratelimit import FakeTime

ROOT = Path(__file__).resolve().parents[1]
SG = {"sg-captcha": "challenge"}


def _price_map(items) -> dict[str, float | None]:
    return {v.variant_id: v.price_usd for i in items for v in i.variants}


# --- 1. WooCommerce bulk: each variation matched by its `parent` --------------------------------------------------


def test_1_an_unlisted_variation_of_a_batch_parent_never_turns_bulk_off():
    """The review's repro: one published, unpriced variation the parent doesn't list (get_visible_children leaves it
    out) used to turn bulk off for the whole store and fall back to one request per size. Its parent is in the
    batch, so it is simply not wanted: every size still comes in bulk."""
    handler, variations = _woo_store(30, 4)
    leftover = {"id": 999_999, "parent": 10_000, "prices": {"price": "", "currency_minor_unit": 2}}

    def with_leftover(req):
        r = handler(req)
        if req.url.params.get("type") == "variation" and "10000" in req.url.params["parent"].split(","):
            return httpx.Response(200, json=[*r.json(), leftover])
        return r

    site = Site(with_leftover)
    with site.client() as c:
        items = woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert _price_map(items) == {str(v["id"]): int(v["prices"]["price"]) / 100 for v in variations}
    assert all(r.url.path.endswith("/products") for r in site.requests)  # no one-by-one lookup
    assert sum(r.url.params.get("type") == "variation" for r in site.requests) == 2  # 120 sizes: 2 batches


# --- 2. One unreadable sub-sitemap doesn't fail the whole fetch ---------------------------------------------------


def _index(*subs: str) -> str:
    body = "".join(f"<sitemap><loc>https://g.com/{s}</loc></sitemap>" for s in subs)
    return f'<?xml version="1.0"?><sitemapindex>{body}</sitemapindex>'


PAGE = '<script type="application/ld+json">{{"@type": "Product", "name": "Coffee {n}", "productID": "{n}"}}</script>'


def _generic_site(tag_status: int, post_status: int):
    def handler(req):
        path = req.url.path
        if path == "/sitemap.xml":
            return httpx.Response(
                200, text=_index("product-sitemap.xml", "tag-sitemap.xml", "post-sitemap.xml")
            )
        if path == "/product-sitemap.xml":
            return httpx.Response(200, text=_urlset([f"https://g.com/product/c{n}" for n in range(3)]))
        if path == "/tag-sitemap.xml":
            return httpx.Response(tag_status)
        if path == "/post-sitemap.xml":
            return httpx.Response(post_status)
        n = path.rsplit("c", 1)[-1]
        return httpx.Response(200, text=PAGE.format(n=n))

    return handler


def test_2_a_stale_404_sub_sitemap_is_skipped_and_the_catalog_is_whole():
    with Site(_generic_site(404, 410)).client() as c:
        items = generic.fetch(c, _r("g.com", "other"))
    assert sorted(i.product_id for i in items) == ["0", "1", "2"]


def test_2_an_unreadable_sub_sitemap_makes_the_catalog_partial_not_failed():
    with Site(_generic_site(404, 500)).client() as c, pytest.raises(PartialCatalog) as e:
        generic.fetch(c, _r("g.com", "other"))
    assert sorted(i.product_id for i in e.value.items) == ["0", "1", "2"]
    assert "post-sitemap.xml (HTTP 500)" in str(e.value)
    unread: list[str] = []
    with Site(_generic_site(404, 500)).client() as c:
        assert len(sitemap.sitemap_urls(c, "g.com", unread)) == 3
    assert unread == ["https://g.com/post-sitemap.xml (HTTP 500)"]


@pytest.mark.parametrize(("status", "headers"), [(429, {}), (202, SG)])
def test_2_a_blocked_or_challenged_sub_sitemap_still_stops_the_fetch(status, headers):
    """A pause or a challenge is the store's answer to us: the store is deferred (or refused), not read in part."""
    handler = _generic_site(404, 404)

    def blocking(req):
        if req.url.path == "/tag-sitemap.xml":
            return httpx.Response(status, headers=headers)
        return handler(req)

    with Site(blocking).client() as c, pytest.raises(FetchError) as e:
        sitemap.sitemap_urls(c, "g.com", [])
    assert block_status(e.value) == status if status == 429 else challenged(e.value)


# --- 3. A partial Shopify fetch keeps its parked progress ---------------------------------------------------------


def _shopify_pages(clock, stop_on: int, last: int):
    def handler(req):
        page = int(req.url.params["page"])
        if page == stop_on:
            clock.t += 10_000  # the deadline passes while this page is read
        n = 0 if page > last else 250
        return httpx.Response(200, json={"products": [{"id": page * 1000 + i, "title": "t", "handle": f"h{i}",
                                                       "variants": []} for i in range(n)]})  # fmt: skip

    return handler


def test_3_a_partial_shopify_catalog_is_parked_and_resumed_from_its_page(lake, clock):
    site = Site(_shopify_pages(clock, stop_on=3, last=5))
    kw = {"min_interval": 0, "retries": 1, "transport": httpx.MockTransport(site)}
    ingest.run(lake, [_r("big.com", "shopify")], "2026-11-02", workers=1, deadline_s=60, client_kw=kw)
    parked = ingest.load_parked(lake, "2026-11-02")
    assert parked["big.com"]["page"] == 4 and len(parked["big.com"]["items"]) == 750
    # the resume slot (connectors.crawl passes load_parked) continues at page 4, not page 1
    site.requests.clear()
    site.handler = _shopify_pages(clock, stop_on=-1, last=5)
    ingest.run(lake, [_r("big.com", "shopify")], "2026-11-02", workers=1, resume=parked, client_kw=kw)
    pages = [int(r.url.params["page"]) for r in site.requests]
    assert pages[0] == 4 and 1 not in pages
    assert not ingest.parked_path(lake, "2026-11-02", "big.com").exists()  # read whole: nothing left parked
    q = f"select status, partial, products from {lake.scan('fetches')} order by fetched_at, partial"
    assert lake.connect().execute(q).fetchall()[-1] == ("ok", False, 1250)


# --- 4. Squarespace doesn't page through a non-shop collection ----------------------------------------------------


def test_4_a_blog_is_read_one_page_then_the_shop():
    urls = [f"https://q.com/blog/post-{i}" for i in range(40)] + [
        f"https://q.com/shop/p/c{i}" for i in range(3)
    ]

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(urls))
        if req.url.path == "/blog":  # a blog's posts: no variants, no productType, and always a next page
            posts = [{"id": f"b{i}", "title": "post", "recordType": 1} for i in range(20)]
            nxt = str(int(req.url.params.get("offset", "0")) + 1)
            return httpx.Response(
                200, json={"items": posts, "pagination": {"nextPage": True, "nextPageOffset": nxt}}
            )
        if req.url.path == "/shop":
            return httpx.Response(200, json={"items": [{"id": "1", "title": "t", "variants": [{"id": "v"}]}]})
        return httpx.Response(404)

    site = Site(handler)
    with site.client() as c:
        assert len(squarespace.fetch(c, _r("q.com", "squarespace"))) == 1
    assert sum(r.url.path == "/blog" for r in site.requests) == 1


# --- 5. crawl-squarespace memory ----------------------------------------------------------------------------------


def test_5_crawl_squarespace_has_8gi():
    tf = (ROOT / "src/terraform/ingestion.tf").read_text()
    assert re.search(r'"crawl-squarespace" += \{.*memory = "8Gi"', tf)


# --- 6. Observability: why a fetch was partial; a store partial every week; RUNBOOK §5.3 ----------------------------


def test_6_a_partial_ok_row_records_why_and_a_failed_row_keeps_its_error():
    r = _r("a.com", "shopify")
    item = woocommerce.CatalogItem(roaster_id="a.com", product_id="1", handle="h", url="u", title="t",
                                   platform="shopify")  # fmt: skip
    now = ingest.datetime(2026, 11, 2, tzinfo=ingest.UTC)
    results = [
        ingest.Result(r, now, items=[item], partial=True, partial_reason="partial: 1 of 2 read"),
        ingest.Result(r, now, error="boom"),
    ]
    rows = ingest.rows_for(results, date(2026, 11, 2), "b", set())["fetches"]
    assert [(x["status"], x["partial"], x["error"]) for x in rows] == [
        ("ok", True, "partial: 1 of 2 read"),
        ("error", None, "boom"),
    ]


def test_6_a_store_partial_in_each_of_its_last_two_runs_is_warned_about(lake):
    for d in ("2026-10-26", "2026-11-02"):
        run(lake, d, "stuck.com", [product("1")], partial=True)
        run(lake, d, "fine.com", [product("1")], partial=True)
    run(lake, "2026-11-02", "fine.com", [product("1")], hour=14)  # read whole later that week
    run(lake, "2026-11-02", "new.com", [product("1")], partial=True)  # only one run so far
    rids = {"stuck.com", "fine.com", "new.com"}
    assert ingest.partial_every_week(lake, "2026-11-02", rids) == ["stuck.com"]


def test_6_runbook_tells_skipped_from_partial():
    text = (ROOT / "docs/ingestion/RUNBOOK.md").read_text()
    section = text[text.index("### 5.3") : text.index("### 5.4")]
    assert "skipped or partial" in section and "partial_every_week" in section
    assert "No re-run is needed" in section


# --- 7. Each connector's documented page size ---------------------------------------------------------------------


def test_7_page_sizes_are_the_documented_maximums_and_are_sent():
    assert (shopify.PAGE_LIMIT, woocommerce.PER_PAGE, square_online.PER_PAGE) == (250, 100, 60)
    src = {m: Path(m.__file__).read_text() for m in (shopify, woocommerce, square_online, squarespace)}
    assert "shopify.dev/docs/api/usage/pagination-rest" in src[shopify]
    assert "Maximum `100`" in src[woocommerce]
    assert "developer.squareup.com" in src[square_online]
    assert "developers.squarespace.com/url-queries" in src[squarespace]
    site = Site(lambda req: httpx.Response(200, json=[]))
    with site.client() as c:
        woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert site.requests[0].url.params["per_page"] == "100"


# --- 8. WooCommerce: the readable size name, not the slug ---------------------------------------------------------

PARENT_ATTRS = [
    {"id": 3, "name": "Size", "taxonomy": "pa_size", "has_variations": True,
     "terms": [{"id": 31, "name": "12 oz", "slug": "12-oz"}, {"id": 32, "name": "0.5 lb", "slug": "0-5-lb"}]},
    {"id": 0, "name": "Grind", "taxonomy": None, "has_variations": True,
     "terms": [{"id": 0, "name": "Whole Bean", "slug": "Whole Bean"}]},
]  # fmt: skip


def test_8_a_variation_label_is_its_terms_name_from_the_parent():
    p = {"id": 1, "name": "Blend", "slug": "blend", "attributes": PARENT_ATTRS, "variations": [
        {"id": 11, "attributes": [{"name": "Size", "value": "12-oz"}, {"name": "Grind", "value": "Whole Bean"}]},
        {"id": 12, "attributes": [{"name": "size", "value": "0-5-lb"}]},  # matched case-insensitively
        {"id": 13, "attributes": [{"name": "Size", "value": "2-lb"}]},  # no such term: kept as it is
        {"id": 14, "attributes": [{"name": "Size", "value": None}]},  # "any"
    ]}  # fmt: skip
    item = woocommerce.parse_product(
        p, _r("w.com", "woocommerce"), dict.fromkeys(["11", "12", "13", "14"], 9.0)
    )
    assert [(v.title, v.grams) for v in item.variants] == [
        ("12 oz Whole Bean", 340), ("0.5 lb", 227), ("2-lb", None), ("", None)
    ]  # fmt: skip


def test_8_eval_readme_records_the_count():
    text = (ROOT / "eval/crawl/README.md").read_text()
    assert "readable size names" in text and "918" in text


# --- 9. A price lookup that fails fails (or defers) the fetch: never a null price --------------------------------


@pytest.mark.parametrize(
    ("status", "headers"), [(202, SG), (202, {}), (429, {}), (503, {}), (404, {}), (500, {})]
)
def test_9_a_failed_one_by_one_lookup_never_stores_a_null_price(status, headers):
    handler, _ = _woo_store(2, 3, ignore_type=True)

    def failing(req):
        if req.url.path.rstrip("/").split("/")[-1].isdigit():
            return httpx.Response(status, headers=headers)
        return handler(req)

    with Site(failing).client() as c, pytest.raises(FetchError) as e:
        woocommerce.fetch(c, _r("w.com", "woocommerce"))
    if status == 202:
        assert challenged(e.value)  # a refusal: counted, never deferred
    elif status in (429, 503):
        assert block_status(e.value) == status and ingest.deferrable(e.value)  # a pause: deferred


@pytest.mark.parametrize("status", [202, 429])
def test_9_a_failed_bulk_page_fails_the_fetch(status):
    handler, _ = _woo_store(2, 3, bulk_status=status)
    with Site(handler).client() as c, pytest.raises(FetchError) as e:
        woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert ingest.blocked(e.value)


# --- 10. X-WP-Total ----------------------------------------------------------------------------------------------


def _woo_counted(n: int, total: str):
    products = [{"id": i, "name": f"C{i}", "slug": f"c{i}", "prices": {"price": "1800"}} for i in range(n)]

    def handler(req):
        per_page, page = int(req.url.params["per_page"]), int(req.url.params["page"])
        # an HTML-escaped Link, as real stores send: never read
        link = f'<{BASE}?page={page + 1}#038;per_page={per_page}>; rel="next"'
        rows = products[(page - 1) * per_page : page * per_page]
        return httpx.Response(200, json=rows, headers={"x-wp-total": total, "link": link})

    return handler


def test_10_a_count_that_differs_from_x_wp_total_is_partial():
    with Site(_woo_counted(150, "2440")).client() as c, pytest.raises(PartialCatalog) as e:
        woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert len(e.value.items) == 150 and "X-WP-Total says 2440" in str(e.value)


def test_10_a_count_that_matches_is_the_whole_catalog():
    with Site(_woo_counted(150, "150")).client() as c:
        assert len(woocommerce.fetch(c, _r("w.com", "woocommerce"))) == 150


# --- 11. A timed-out page is asked for again in smaller pages ------------------------------------------------------


def test_11_a_page_that_times_out_is_read_in_smaller_pages():
    """volunteercoffee.com: 41 s for 100 products. The same products come back 25 to a page, from the same offset."""
    products = [{"id": i, "name": f"C{i}", "slug": f"c{i}", "prices": {"price": "1800"}} for i in range(160)]

    def handler(req):
        per_page, page = int(req.url.params["per_page"]), int(req.url.params["page"])
        if per_page > 25 and page > 1:  # the second 100 is too heavy
            raise httpx.ReadTimeout("timed out", request=req)
        return httpx.Response(200, json=products[(page - 1) * per_page : page * per_page])

    site = Site(handler)
    with site.client() as c:
        items = woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert sorted(int(i.product_id) for i in items) == list(range(160))
    asked = [(r.url.params["per_page"], r.url.params["page"]) for r in site.requests]
    assert asked == [("100", "1"), ("100", "2"), ("25", "5"), ("25", "6"), ("25", "7")]


# --- 12. Each variation's own stock ------------------------------------------------------------------------------


def test_12_a_variation_takes_its_own_is_in_stock_from_the_bulk_response():
    handler, variations = _woo_store(1, 3)
    out_of_stock = variations[1]["id"]

    def stock(req):
        r = handler(req)
        if req.url.params.get("type") == "variation":
            return httpx.Response(200, json=[v | {"is_in_stock": v["id"] != out_of_stock} for v in r.json()])
        return httpx.Response(200, json=[p | {"is_in_stock": True} for p in r.json()])

    with Site(stock).client() as c:
        (item,) = woocommerce.fetch(c, _r("w.com", "woocommerce"))
    assert [v.available for v in item.variants] == [True, False, True]


# --- 13. RateLimit-Retry-After, under the 5-minute cap, and jittered backoffs --------------------------------------


def test_13_ratelimit_retry_after_is_read_and_backoffs_are_jittered(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    monkeypatch.setattr(http, "_random", lambda: 1.0)  # the most jitter: +25%
    answers = iter([
        httpx.Response(429, headers={"ratelimit-retry-after": "8"}),  # the Store API's own header
        httpx.Response(503),  # no wait given: 2 s, jittered
        httpx.Response(200, json=[]),
    ])  # fmt: skip
    with respx.mock as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get(BASE).mock(side_effect=lambda req: next(answers))
        with PoliteClient(min_interval=0, retries=3) as c:
            assert c.fetch_json(BASE) == []
    assert sleeps == [10.0, 2.5]
    long = httpx.Response(429, headers={"ratelimit-retry-after": "7200"})
    assert http.asked_wait(long) == 7200 and http.store_wait(http.asked_wait(long)) == http.MAX_STORE_WAIT


# --- 14. On the last try, a store's own pause is waited out (at most 5 minutes) -------------------------------------


def test_14_the_last_try_waits_out_a_long_host_pause_and_earlier_tries_defer():
    clock = FakeTime()
    lim = PlatformLimiter("squarespace", rate=100, max_concurrent=1, per_host=True,
                          clock=clock.monotonic, sleep=clock.sleep)  # fmt: skip
    lim.defer = False  # a deferred round
    lim.record(
        httpx.Response(429, headers={"retry-after": "7200"}, request=httpx.Request("GET", "https://q.com/"))
    )
    with pytest.raises(HostRateLimited), lim.slot("q.com"):  # not the last try: over max_cooldown, deferred
        pass
    with lim.slot("q.com", patient=True):  # the last try: waits out the pause, capped at MAX_STORE_WAIT
        pass
    assert max(clock.sleeps) == pytest.approx(http.MAX_STORE_WAIT)


# --- 15. A header-only catalog challenge is a challenge --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers"),
    [(200, SG), (403, {"cf-mitigated": "challenge"}), (503, {"cf-mitigated": "challenge"})],
)
def test_15_a_challenge_header_on_any_status_is_a_challenge_and_never_retried(status, headers):
    with respx.mock as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        route = m.get(BASE).mock(return_value=httpx.Response(status, headers=headers, text="<html>"))
        with PoliteClient(min_interval=0, retries=3) as c, pytest.raises(FetchError) as e:
            c.fetch_json(BASE)
    assert challenged(e.value) and not ingest.deferrable(e.value) and ingest.blocked(e.value)
    assert route.call_count == 1


# --- 16. A 202 on a generic product page is a block ----------------------------------------------------------------


def test_16_a_challenged_product_page_stops_the_fetch_as_a_refusal():
    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset(["https://g.com/product/a", "https://g.com/product/b"]))
        return httpx.Response(202, headers=SG)

    site = Site(handler)
    with site.client() as c, pytest.raises(FetchError) as e:
        generic.fetch(c, _r("g.com", "other"))
    assert challenged(e.value) and ingest.blocked(e.value) and "failed" not in str(e.value)
    assert len(site.requests) == 2  # the sitemap, then one page: no reading on past a refusal


# --- 17. Lone Oak's products in the running total --------------------------------------------------------------------


def test_17_lone_oak_is_in_the_crawl_running_total():
    text = (ROOT / "eval/crawl/README.md").read_text()
    assert re.search(r"Lone Oak.*\| 37 \|", text)


# --- 18. registry.current never returns a NOT_A_ROASTER_SITE host ---------------------------------------------------


def test_18_a_lake_registry_row_for_a_vendor_site_is_never_a_roaster(lake):
    from datetime import UTC, datetime

    lake.write("registry", "registry/source=manual/x.parquet", [
        {"added_at": datetime.now(UTC), "source": "manual", "roaster_id": rid, "name": "Vendor"}
        for rid in ("tryperdiem.com", "us.orderspoon.com", "realroaster.com")
    ])  # fmt: skip
    ids = {s.roaster.roaster_id for s in registry.current(lake)}
    assert "realroaster.com" in ids
    assert not ids & {"tryperdiem.com", "us.orderspoon.com", "gmail.com"}


# --- 19. Discovery: a challenged robots.txt is `blocked`, not re-probed every run -----------------------------------


@pytest.mark.parametrize(("status", "headers"), [(403, SG), (401, {"cf-mitigated": "challenge"}), (202, {})])
def test_19_a_challenged_robots_txt_probes_as_blocked(status, headers):
    with respx.mock as m:
        m.get(url__regex=r"https://(www\.)?a\.com/robots\.txt").mock(
            return_value=httpx.Response(status, headers=headers)
        )
        with PoliteClient(min_interval=0, retries=1) as c:
            out = discover.probe_one(c, "a.com")
    assert out["status"] == "blocked"


# --- Registry fixes by hand ------------------------------------------------------------------------------------------


def test_registry_fixes_martin_pic_and_mean_bean():
    seed = {r["roaster_id"]: r for r in registry.load_seed()}
    got = {rid: (seed[rid]["final_host"], seed[rid]["platform"], seed[rid]["last_probed"])
           for rid in ("martincoffee.com", "piccoffee.com", "meanbeanroasters.com")}  # fmt: skip
    assert got == {
        "martincoffee.com": (
            "martincoffee.com",
            "shopify",
            "2026-11-02",
        ),  # 302 to /password: closed (unavailable)
        "piccoffee.com": ("piccoffee.com", "other", "2026-11-02"),  # no Store API: WordPress on Flywheel
        "meanbeanroasters.com": ("www.meanbeancoffeeroasters.com", "http_404", "2026-11-02"),  # unreachable
    }


def test_merge_keeps_the_deadline_with_the_store_wait_cap(clock):
    """#98's 5-minute cap and #99's deadline together: a request that waited out a pause past the deadline isn't sent."""
    lim = PlatformLimiter("shopify", rate=100, max_concurrent=1, per_host=True)
    lim.defer = False
    site = Site(lambda req: httpx.Response(200, json={"products": []}))
    with site.client(deadline_s=60, limiters={"shopify": lim}) as c:
        view = c.for_platform("shopify")
        lim.host_until["s.com"] = 0.0
        # the limiter's pause passes the deadline (the fake clock jumps while the slot is taken)
        orig = lim._wait_for_slot

        def late() -> None:
            orig()
            clock.t += 120

        lim._wait_for_slot = late  # type: ignore[method-assign]
        with pytest.raises(http.DeadlineReached):
            view.get("https://s.com/products.json")
    assert site.requests == []
