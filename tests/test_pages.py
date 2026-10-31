"""The secondary crawler (pages.py), offline against saved page fixtures (tests/fixtures/pages/README.md)."""

from datetime import UTC, date, datetime
from urllib.parse import unquote, urlparse

import httpx
import pytest
import respx

from coffeedata import extract, ingest, pages, registry
from coffeedata.adapters import shopify
from coffeedata.classify import is_coffee
from coffeedata.http import RequestStats
from coffeedata.lake import new_batch_id
from coffeedata.models import Roaster

from .conftest import FIX, load

PAGES = FIX / "pages"
HOST = "counterculturecoffee.com"
CCC = Roaster(roaster_id=HOST, name="Counter Culture", host=HOST, platform="shopify")


def _store(request: httpx.Request) -> httpx.Response:
    """counterculturecoffee.com, served from the fixtures: sitemap, product sitemap and product pages."""
    path = unquote(urlparse(str(request.url)).path)
    if path == "/sitemap.xml":
        return httpx.Response(200, text=(PAGES / "ccc_sitemap.xml").read_text())
    if path == "/sitemap_products_1.xml":
        return httpx.Response(200, text=(PAGES / "ccc_sitemap_products_1.xml").read_text())
    if path.startswith("/products/") and (f := PAGES / f"ccc_{path.rsplit('/', 1)[-1]}.html").exists():
        return httpx.Response(200, text=f.read_text())
    return httpx.Response(404)


@pytest.fixture
def store():
    with respx.mock(assert_all_called=False) as m:
        m.get(f"https://{HOST}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /cart\nDisallow: /checkout\n")
        )
        route = m.get(url__regex=rf"https://{HOST}/.*").mock(side_effect=_store)
        yield m, route


def _crawl():
    return pages.client(interval=0)  # no real waiting in tests


def test_same_records_as_the_json_adapter(store):
    """The page path yields the JSON adapter's product ids, variant ids, prices and stock for every coffee."""
    json_items = {
        i.product_id: i
        for i in (
            shopify.parse_product(p, CCC) for p in load("shopify_counterculturecoffee.json")["products"]
        )
    }
    with _crawl() as c:
        rep = pages.Report()
        items = pages.fetch(c, CCC, report=rep)
    by_id = {i.product_id: i for i in items}
    coffees = {pid for pid, i in json_items.items() if is_coffee(i)}
    assert coffees and coffees <= by_id.keys()
    # Not on a product page: the photo (never stored here) and the publish time; tags and per-variant grams only
    # when the theme embeds its product JSON.
    unread = {"image", "payload_hash", "body_hash", "published_at"}
    for pid in coffees:
        a, b = json_items[pid].model_dump(exclude=unread), by_id[pid].model_dump(exclude=unread)
        if b["tags"] == []:
            a["tags"] = []
            a["variants"] = [v | {"grams": None} for v in a["variants"]]
            b["variants"] = [v | {"grams": None} for v in b["variants"]]
        assert a == b, pid
    assert rep.source == "sitemap" and rep.listed == 12
    # Merch and gear named in the sitemap's image titles are never fetched (mug, hat, tote, hoodie, grinder).
    assert rep.skipped_not_coffee == 5 and rep.fetched == 7


def test_requests_are_sitemap_then_pages_only(store):
    m, route = store
    with _crawl() as c, c.track() as stats:
        pages.fetch(c, CCC)
    paths = [urlparse(str(call.request.url)).path for call in route.calls]
    assert paths[:2] == [
        "/sitemap.xml",
        "/sitemap_products_1.xml",
    ]  # pages, collections and blogs maps skipped
    assert all(p.startswith("/products/") for p in paths[2:])
    assert stats.requests == 1 + 2 + 7  # robots.txt, two sitemaps, seven product pages
    assert {call.request.headers["user-agent"] for call in route.calls} == {pages.PAGES_USER_AGENT}
    assert "@" in pages.PAGES_USER_AGENT or "support" in pages.PAGES_USER_AGENT  # a contact, no disguise


def test_facts_only_no_description_or_image(store):
    with _crawl() as c:
        items = {i.handle: i for i in pages.fetch(c, CCC)}
    honey = items["finca-el-puente-honey"]
    assert honey.image is None
    assert honey.body_text == (
        "Origin: Marcala, Honduras\nProcess: Honey\nVariety: Catuai, Lempira\nTasting Notes: Peach, Honey, Black Tea"
    )
    e = extract.rules(honey)
    assert (e.origin_country, e.process, e.tasting_notes[:1]) == ("Honduras", "honey", ["peach"])
    assert "Catuai" in e.varietals
    # A page without spec lines keeps no text at all, even though its description is on the page.
    assert items["laba-gesha-natural-sundried"].body_text == ""


def test_theme_product_json_brings_tags_and_type(store):
    with _crawl() as c:
        worka = next(i for i in pages.fetch(c, CCC) if i.handle == "worka-chelbessa")
    assert "roastlevel__light-roast" in worka.tags and worka.product_type == "Coffee"
    assert extract.rules(worka).roast_level == "light"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"retry-after": "60"}),
        httpx.Response(403),
        httpx.Response(430),
        httpx.Response(200, text=(PAGES / "challenge.html").read_text()),
    ],
)
def test_a_block_stops_the_store_at_once(store, response):
    m, route = store
    route.side_effect = None
    real = _store

    def blocked_on_pages(request):
        return response if "/products/" in str(request.url) else real(request)

    route.side_effect = blocked_on_pages
    with _crawl() as c, pytest.raises(pages.Refused):
        pages.fetch(c, CCC)
    product_calls = [call for call in route.calls if "/products/" in str(call.request.url)]
    assert len(product_calls) == 1  # no retry, no next page


def test_robots_disallow_is_honored(store):
    m, route = store
    m.get(f"https://{HOST}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: coffee-data\nDisallow: /\n")
    )
    with _crawl() as c, pytest.raises(pages.FetchError):
        pages.fetch(c, CCC)
    assert not route.calls


def test_a_missing_page_is_delisted_but_an_unreadable_one_fails_the_store(store):
    m, route = store

    def gone(request):
        return httpx.Response(404) if "worka" in str(request.url) else _store(request)

    route.side_effect = gone
    with _crawl() as c:
        assert "worka-chelbessa" not in {i.handle for i in pages.fetch(c, CCC)}

    def broken(request):
        return httpx.Response(500) if "worka" in str(request.url) else _store(request)

    route.side_effect = broken
    with _crawl() as c, pytest.raises(pages.FetchError, match="1 of 7"):
        pages.fetch(c, CCC)


def test_collection_pages_when_the_sitemap_has_no_products(store):
    m, route = store
    listing = "".join(f'<a href="/products/{h}">x</a>' for h in ("finca-el-puente-honey", "worka-chelbessa"))

    def no_sitemap(request):
        path = urlparse(str(request.url)).path
        if path == "/sitemap.xml":
            return httpx.Response(404)
        if path == "/collections/all":
            return httpx.Response(200, text=listing if "page=" not in str(request.url) else "")
        return _store(request)

    route.side_effect = no_sitemap
    with _crawl() as c:
        rep = pages.Report()
        items = pages.fetch(c, CCC, report=rep)
    assert rep.source == "collections" and len(items) == 2


def test_renderer_only_for_pages_without_product_data(store):
    m, route = store
    worka = (PAGES / "ccc_worka-chelbessa.html").read_text()

    def bare(request):
        return (
            httpx.Response(200, text="<html><body>loading</body></html>")
            if "worka" in str(request.url)
            else _store(request)
        )

    route.side_effect = bare
    rendered = []
    with _crawl() as c:
        items = pages.fetch(c, CCC, render=lambda url: rendered.append(url) or worka)
    assert f"https://{HOST}/products/worka-chelbessa" in rendered  # no product data in its server HTML
    assert "worka-chelbessa" in {i.handle for i in items}


def test_renderer_for_a_coffee_whose_facts_scripts_build(store):
    """Price and stock are in the server HTML, but the spec panel isn't: only the rendered page has the facts."""
    m, route = store
    rendered = []

    def render(url):
        rendered.append(url)
        page = _store(httpx.Request("GET", url)).text
        return page.replace("<main", '<div class="stats"><div>Process</div><div>Natural</div></div><main')

    with _crawl() as c:
        rep = pages.Report()
        items = {i.handle: i for i in pages.fetch(c, CCC, render=render, report=rep)}
    # Coffee pages without spec lines are rendered; ones with spec lines, and non-coffee (the Bambino), are not.
    assert f"https://{HOST}/products/finca-el-puente-honey" not in rendered
    assert f"https://{HOST}/products/the-bambino%E2%84%A2" not in rendered and len(rendered) == 3
    assert items["laba-gesha-natural-sundried"].body_text == "Process: Natural"
    assert rep.facts_from_render == 3


def test_woocommerce_pages():
    backstage = Roaster(
        roaster_id="anaheimcoffee.com", name="A", host="anaheimcoffee.com", platform="woocommerce"
    )
    simple = pages.parse_page(
        (PAGES / "woo_simple.html").read_text(), "https://anaheimcoffee.com/product/mexico/", backstage
    )
    woo_json = load("woocommerce_anaheimcoffee.json")[0]
    assert simple.product_id == str(woo_json["id"]) and simple.platform == "woocommerce"
    assert simple.variants[0].price_usd == int(woo_json["prices"]["price"]) / 100
    assert simple.body_text == "Origin: Chiapas, Mexico\nRoast: Medium"
    var = pages.parse_page(
        (PAGES / "woo_variable.html").read_text(), "https://x.com/product/guji/", backstage
    )
    assert [(v.variant_id, v.price_usd, v.available, v.grams) for v in var.variants] == [
        ("901", 18.0, True, 340),
        ("902", 70.0, False, 2268),
    ]
    e = extract.rules(var)
    assert (e.process, e.tasting_notes) == ("natural", ["blueberry", "jasmine"])


def test_challenge_detection_ignores_product_pages_that_load_a_captcha():
    assert pages.is_challenge((PAGES / "challenge.html").read_text())
    form = '<script src="https://www.google.com/recaptcha/api.js"></script><div class="g-recaptcha"></div>'
    assert not pages.is_challenge((PAGES / "ccc_finca-el-puente-honey.html").read_text() + form)
    assert pages.is_challenge("<html><title>Access Denied</title><body>Reference #18.abc</body></html>")


def test_run_writes_the_json_adapters_shape(store, lake):
    summary, out = pages.run(lake, [CCC], "2026-11-02", interval=0)
    assert summary["ok"] == 1 and summary["products"] == 7
    con = lake.connect()
    snap = con.execute(f"select * from {lake.scan('snapshots')}")
    cols = [d[0] for d in snap.description]
    rows = [dict(zip(cols, r, strict=True)) for r in snap.fetchall()]
    assert set(cols) >= {"roaster_id", "product_id", "variants", "payload_hash", "is_coffee"}
    assert all(r["image"] is None for r in rows) and sum(r["is_coffee"] for r in rows) == 6
    fetch = con.execute(f"select status, requests, run_date from {lake.scan('fetches')}").fetchall()
    assert fetch == [("ok", 10, date(2026, 11, 2))]


def test_only_rate_limited_stores_are_selected_never_blocked_ones(lake, monkeypatch):
    from collections import Counter

    now = datetime.now(UTC)

    def res(rid, statuses=(), error="refused: answered a block to every catalog request"):
        r = Roaster(roaster_id=rid, name=rid, host=rid, platform="shopify")
        stats = RequestStats(statuses=Counter(statuses), rate_limited=list(statuses).count(429))
        return ingest.Result(r, now, error=error, stats=stats)

    results = [
        res("limited.com", [429, 429, 429]),
        res("recovered.com", [429]),
        res("recovered.com", [200], error=None),  # ok later in the run
        res("blocked430.com", [430, 430]),  # Shopify's security block
        res("forbidden.com", [403]),
        res("mixed.com", [429, 403]),
        res("down.com", [], error="ConnectError"),
    ]
    rows = ingest.rows_for(results, date(2026, 11, 2), new_batch_id(), set(), set())
    lake.write_partition("fetches", "2026-11-02", new_batch_id(), rows["fetches"])
    states = [registry.RoasterState(roaster=r.roaster, status="active", last_probed=None) for r in results]
    monkeypatch.setattr(registry, "current", lambda _lake: states)
    assert [r.roaster_id for r in pages.rate_limited(lake, "2026-11-02")] == ["limited.com"]
