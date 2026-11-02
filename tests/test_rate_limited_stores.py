"""The owner, on PR #98's review: no connector gives up on a store after a 429, 430 or 503. The store is deferred and
asked again in the job's later rounds, never sooner than its Retry-After (and after a backoff when it gives none),
and ends failed or parked only once the retries are spent. Each case below is a store that answers 429 twice, with a
Retry-After longer than the client waits between its own tries (http.MAX_RETRY_WAIT), and then its catalog: it ends
ok on every connector, through `crawl` with the connector's real config."""

import functools
import json

import httpx
import pytest
import respx

from coffeedata import connectors, http, ingest, registry
from coffeedata.models import Roaster
from coffeedata.ratelimit import PlatformLimiter
from tests.test_ratelimit import FakeTime

WEEK = "2026-11-02"
WAIT = "120"  # seconds: longer than http.MAX_RETRY_WAIT, so the client stops and the store must be deferred


def busy(*then: httpx.Response) -> list[httpx.Response]:
    return [
        httpx.Response(429, headers={"retry-after": WAIT}),
        httpx.Response(429, headers={"retry-after": WAIT}),
        *then,
    ]


@pytest.fixture
def clock(monkeypatch) -> FakeTime:
    """One fake clock for ingest's rounds and the limiters' pauses; every wait passes at once and is recorded."""
    clock = FakeTime()
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "203.0.113.9")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(ingest, "time", clock)
    monkeypatch.setattr(
        ingest,
        "PlatformLimiter",
        functools.partial(PlatformLimiter, clock=clock.monotonic, sleep=clock.sleep),
    )
    return clock


def crawl_one(lake, monkeypatch, connector: str, host: str, platform: str) -> dict:
    store = Roaster(roaster_id=host, name="R", host=host, platform=platform)
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(store, "active", None)]
    )
    return connectors.crawl(lake, connector, WEEK)


def fetches(lake, connector: str, rid: str, col: str = "status") -> list:
    """The store's fetches rows this week, in order: in the connector job's own partition until the merge."""
    part = connectors.PartitionLake(lake, connector, WEEK)
    q = f"select {col} from {part.scan('fetches')} where roaster_id = ? order by fetched_at, status"
    return [r[0] for r in part.connect().execute(q, [rid]).fetchall()]


def test_shopify(lake, monkeypatch, clock):
    with respx.mock(assert_all_called=False) as m:
        m.get("https://s.com/robots.txt").mock(return_value=httpx.Response(404))
        page1 = m.get("https://s.com/products.json", params={"page": "1"})
        page1.side_effect = busy(httpx.Response(200, json={"products": [{"id": 1, "title": "Blend"}]}))
        m.get("https://s.com/products.json", params={"page": "2"}).mock(
            return_value=httpx.Response(200, json={"products": []})
        )
        out = crawl_one(lake, monkeypatch, "shopify", "s.com", "shopify")
    assert fetches(lake, "shopify", "s.com")[-1] == "ok"
    assert out["deferred"]["page_backoffs"] == 2  # its page backed off twice, then served
    assert any(s >= 120 for s in clock.sleeps)  # never asked again before its Retry-After


def test_squarespace(lake, monkeypatch, clock):
    item = {"id": "a1", "title": "Blend", "urlId": "blend", "variants": [{"price": 1800}]}
    with respx.mock(assert_all_called=False) as m:
        m.get("https://q.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://q.com/sitemap.xml").mock(return_value=httpx.Response(404))
        m.get("https://q.com/shop").mock(side_effect=busy(httpx.Response(200, json={"items": [item]})))
        out = crawl_one(lake, monkeypatch, "squarespace", "q.com", "squarespace")
    assert fetches(lake, "squarespace", "q.com")[-1] == "ok"
    assert out["deferred"]["round_1"] == 1  # deferred, not given up on
    assert out["squarespace_limiter"]["rate_limited"] == 2  # the job's own limiter, in its manifest
    assert "shopify_limiter" not in out
    assert any(s >= 120 for s in clock.sleeps)


def test_woocommerce(lake, monkeypatch, clock):
    with respx.mock(assert_all_called=False) as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://w.com/wp-json/wc/store/v1/products").mock(
            side_effect=busy(
                httpx.Response(200, json=[{"id": 7, "name": "Blend", "permalink": "https://w.com/p/blend"}])
            )
        )
        out = crawl_one(lake, monkeypatch, "woocommerce", "w.com", "woocommerce")
    assert fetches(lake, "woocommerce", "w.com") == ["error", "error", "ok"]
    assert out["deferred"]["round_2"] == 1
    assert any(s >= 120 for s in clock.sleeps)


def test_square_online(lake, monkeypatch, clock):
    home = '<script>{"user":{"id":"123"},"site_id":"1234567"}</script>'
    api = "https://cdn5.editmysite.com/app/store/api/v28/editor/users/123/sites/1234567/products"
    with respx.mock(assert_all_called=False) as m:
        m.get("https://sq.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://cdn5.editmysite.com/robots.txt").mock(return_value=httpx.Response(400))
        m.get("https://sq.com/").mock(side_effect=busy(httpx.Response(200, text=home)))
        m.get(url__startswith=api).mock(return_value=httpx.Response(200, json={"data": [], "meta": {}}))
        crawl_one(lake, monkeypatch, "square_online", "sq.com", "square_online")
    assert fetches(lake, "square_online", "sq.com") == ["error", "error", "ok"]


def test_generic_product_page(lake, monkeypatch, clock):
    """A product page's 429 used to count as an unreadable page: the fetch failed, not deferrable, for the week."""
    ld = json.dumps({"@type": "Product", "name": "House Blend", "offers": {"price": "18.00"}})
    page = f'<script type="application/ld+json">{ld}</script>'
    sitemap = "<urlset><url><loc>https://g.com/products/house-blend</loc></url></urlset>"
    with respx.mock(assert_all_called=False) as m:
        m.get("https://g.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://g.com/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap))
        m.get("https://g.com/products/house-blend").mock(side_effect=busy(httpx.Response(200, text=page)))
        crawl_one(lake, monkeypatch, "generic", "g.com", "other")
    assert fetches(lake, "generic", "g.com") == ["error", "error", "ok"]
    assert fetches(lake, "generic", "g.com", "products")[-1] == 1


def test_a_rate_limited_robots_txt_is_deferred_not_given_up_on(lake, monkeypatch, clock):
    """robots.txt's own 429 used to be a non-answer (RobotsUnavailable without a status): not deferrable. It is
    remembered for its Retry-After (on the client's clock, here the same fake one), not for ROBOTS_RETRY_AFTER."""
    monkeypatch.setattr(http.time, "monotonic", clock.monotonic)
    with respx.mock(assert_all_called=False) as m:
        m.get("https://r.com/robots.txt").mock(side_effect=busy(httpx.Response(404)))
        m.get("https://r.com/wp-json/wc/store/v1/products").mock(return_value=httpx.Response(200, json=[]))
        crawl_one(lake, monkeypatch, "woocommerce", "r.com", "woocommerce")
    assert fetches(lake, "woocommerce", "r.com") == ["error", "error", "ok"]


def test_a_woocommerce_variation_lookup_429_defers_the_store_instead_of_dropping_the_price(
    lake, monkeypatch, clock
):
    product = {
        "id": 7,
        "name": "Blend",
        "permalink": "https://v.com/p/blend",
        "type": "variable",
        "variations": [{"id": 8, "attributes": [{"name": "Size", "value": "12 oz"}]}],
        "prices": {
            "price": "1800",
            "currency_minor_unit": 2,
            "price_range": {"min_amount": "1800", "max_amount": "3400"},
        },
    }
    variation = {"id": 8, "prices": {"price": "1800", "currency_minor_unit": 2}}
    with respx.mock(assert_all_called=False) as m:
        m.get("https://v.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://v.com/wp-json/wc/store/v1/products").mock(
            return_value=httpx.Response(200, json=[product])
        )
        lookup = m.get("https://v.com/wp-json/wc/store/v1/products/8")
        lookup.side_effect = busy(httpx.Response(200, json=variation))
        crawl_one(lake, monkeypatch, "woocommerce", "v.com", "woocommerce")
    assert lookup.call_count == 3  # once per round: its 429s deferred the store, never a missing price
    assert fetches(lake, "woocommerce", "v.com") == ["error", "error", "ok"]


def test_a_store_that_never_stops_answering_429_ends_failed_after_its_rounds(lake, monkeypatch, clock):
    with respx.mock(assert_all_called=False) as m:
        m.get("https://n.com/robots.txt").mock(return_value=httpx.Response(404))
        route = m.get("https://n.com/wp-json/wc/store/v1/products").mock(
            return_value=httpx.Response(429, headers={"retry-after": WAIT})
        )
        crawl_one(lake, monkeypatch, "woocommerce", "n.com", "woocommerce")
    assert fetches(lake, "woocommerce", "n.com") == ["error"] * (ingest.DEFERRED_ROUNDS + 1)
    assert route.call_count == ingest.DEFERRED_ROUNDS + 1  # one ask per round: never sooner than Retry-After
