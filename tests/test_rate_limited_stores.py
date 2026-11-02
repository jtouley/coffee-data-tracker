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


# --- PR #98's final review, and the owner's rule: we wait at most 5 minutes for anything a store asks for ----------


def _woo_store(host: str = "w.com") -> Roaster:
    return Roaster(roaster_id=host, name="R", host=host, platform="woocommerce")


def _statuses(lake, rid: str) -> list[str]:
    q = f"select status from {lake.scan('fetches')} where roaster_id = ? order by fetched_at, status"
    return [r[0] for r in lake.connect().execute(q, [rid]).fetchall()]


WOO_OK = [{"id": 7, "name": "Blend", "permalink": "https://w.com/p/blend"}]
HOURS = "7200"


@pytest.mark.parametrize("where", ["catalog", "robots"])
def test_a_retry_after_of_hours_is_waited_for_5_minutes_and_the_store_asked_again(
    lake, monkeypatch, clock, where
):
    """Reproduced on ef72b9e: one store answering 429 + Retry-After: 7200 held a run without a deadline (weekly's
    cmd_ingest, crawl-retry) for 4 hours of deferred rounds. Now its wait is cut to 300 s: it is asked again in the
    next deferred round, never failed just for asking, and the run ends within minutes."""
    monkeypatch.setattr(http.time, "monotonic", clock.monotonic)  # robots.txt's pause, on the same clock
    busy = httpx.Response(429, headers={"retry-after": HOURS})
    with respx.mock(assert_all_called=False) as m:
        robots = m.get("https://w.com/robots.txt")
        catalog = m.get("https://w.com/wp-json/wc/store/v1/products")
        if where == "robots":
            robots.side_effect = [busy, httpx.Response(404)]
            catalog.mock(return_value=httpx.Response(200, json=WOO_OK))
        else:
            robots.mock(return_value=httpx.Response(404))
            catalog.side_effect = [busy, httpx.Response(200, json=WOO_OK)]
        out = ingest.run(
            lake, [_woo_store()], WEEK, workers=2
        )  # as cmd_ingest and crawl-retry ran: no deadline
    assert _statuses(lake, "w.com") == ["error", "ok"]
    assert out["deferred"]["round_1"] == 1
    assert max(clock.sleeps) == http.MAX_STORE_WAIT  # asked again after 300 s: never sooner, never later
    assert sum(clock.sleeps) <= http.MAX_STORE_WAIT + ingest.DEFERRED_SPREAD
    assert (robots if where == "robots" else catalog).call_count == 2


def test_a_store_that_keeps_asking_for_hours_is_asked_every_round_and_the_run_ends_in_minutes(lake, clock):
    with respx.mock(assert_all_called=False) as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        catalog = m.get("https://w.com/wp-json/wc/store/v1/products").mock(
            return_value=httpx.Response(503, headers={"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
        )
        out = ingest.run(lake, [_woo_store()], WEEK, workers=2)
    assert catalog.call_count == ingest.DEFERRED_ROUNDS + 1  # never given up on before its rounds are spent
    assert (
        clock.sleeps == [http.MAX_STORE_WAIT] * ingest.DEFERRED_ROUNDS
    )  # an HTTP date years away: 300 s too
    assert out["deferred"]["still_failing"] == 1


def test_a_limiter_pause_and_a_shopify_page_backoff_are_capped_too(lake, monkeypatch, clock):
    """The limiter's pause for one store (HostRateLimited) and the page backoffs took a Retry-After uncapped: three
    backoffs at 7200 s would have held the job 6 hours."""
    with respx.mock(assert_all_called=False) as m:
        m.get("https://s.com/robots.txt").mock(return_value=httpx.Response(404))
        page1 = m.get("https://s.com/products.json", params={"page": "1"})
        page1.side_effect = [httpx.Response(429, headers={"retry-after": HOURS})] * 3 + [
            httpx.Response(200, json={"products": [{"id": 1, "title": "Blend"}]})
        ]
        m.get("https://s.com/products.json", params={"page": "2"}).mock(
            return_value=httpx.Response(200, json={"products": []})
        )
        out = crawl_one(lake, monkeypatch, "shopify", "s.com", "shopify")
    assert fetches(lake, "shopify", "s.com")[-1] == "ok"
    assert out["deferred"]["page_backoffs"] >= 1
    assert max(clock.sleeps) <= http.MAX_STORE_WAIT
    limiter = PlatformLimiter("shopify", rate=1000, max_concurrent=1, per_host=True, clock=clock.monotonic)
    request = httpx.Request("GET", "https://x.com/")
    limiter.record(httpx.Response(429, headers={"retry-after": HOURS}, request=request))
    assert limiter.host_until["x.com"] - clock.monotonic() == pytest.approx(http.MAX_STORE_WAIT)


def test_a_deferred_retry_is_never_scheduled_past_the_deadline(lake, clock):
    """Its wait ends after the run's deadline: the store keeps its error row for the next pass, at once, instead of
    the run idling to its deadline."""
    with respx.mock(assert_all_called=False) as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        catalog = m.get("https://w.com/wp-json/wc/store/v1/products").mock(
            return_value=httpx.Response(429, headers={"retry-after": HOURS})
        )
        out = ingest.run(lake, [_woo_store()], WEEK, workers=2, deadline_s=120)
    assert sum(clock.sleeps) == 0
    assert out["deferred"] == {"past_deadline": 1} and out["skipped_deadline"] == 0
    assert "past the run's deadline" in out["failures"][0]
    assert catalog.call_count == 1


def test_crawl_retry_and_the_weekly_fallback_have_deadlines_inside_their_240_minutes(lake, monkeypatch):
    """Both called ingest.run without one: nothing stopped them before the 240-minute kill (ingestion.tf's
    crawl-retry task timeout, weekly.yml's timeout-minutes)."""
    import argparse

    from coffeedata import cli

    seen: list = []
    monkeypatch.setattr(ingest, "run", lambda *a, **k: seen.append(k.get("deadline_s")) or {"deferred": {}})
    monkeypatch.setattr(ingest, "select_parked", lambda *a, **k: ([], {}))
    connectors.crawl_retry(lake, "shopify", WEEK)
    assert seen == [60 * connectors.CONNECTORS["shopify"].deadline_min] and seen[0] < 240 * 60
    seen.clear()
    cli.cmd_crawl_retry(argparse.Namespace(run_date=WEEK, workers=8, connector=None, deadline_min=None))
    assert seen == [60 * cli.CRAWL_RETRY_MIN] and cli.CRAWL_RETRY_MIN < 240

    calls: list = []
    for name in ("cmd_probe", "cmd_enrich", "cmd_classify", "cmd_publish", "_export_and_site"):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    monkeypatch.setattr(cli, "cmd_build", lambda a: True)
    monkeypatch.setattr(cli, "cmd_ingest", lambda a: calls.append(a.deadline_min))
    cli.cmd_weekly(argparse.Namespace(run_date="2026-11-09", discover=False, sample=None, enrich_limit=None,
                                      db="build/coffee.duckdb", out="build/site"))  # fmt: skip
    assert len(calls) == 2 and all(0 < d <= cli.WEEKLY_CRAWL_MIN < 240 for d in calls)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_5xx_with_a_long_retry_after_is_deferred_like_a_429(lake, clock, status):
    """Reproduced on ef72b9e: the client stops at a Retry-After over MAX_RETRY_WAIT, and a 5xx then failed the store
    after one request (main retried it). It is now deferred past its Retry-After, as a 429 is, capped at 300 s."""
    with respx.mock(assert_all_called=False) as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        route = m.get("https://w.com/wp-json/wc/store/v1/products")
        route.side_effect = [
            httpx.Response(status, headers={"retry-after": HOURS}),
            httpx.Response(200, json=WOO_OK),
        ]
        out = ingest.run(lake, [_woo_store()], WEEK, workers=2)
    assert _statuses(lake, "w.com") == ["error", "ok"]
    assert route.call_count == 2 and out["deferred"]["round_1"] == 1
    assert max(clock.sleeps) == http.MAX_STORE_WAIT


def test_a_5xx_robots_txt_with_a_long_retry_after_is_deferred_like_a_429(lake, clock, monkeypatch):
    monkeypatch.setattr(http.time, "monotonic", clock.monotonic)
    with respx.mock(assert_all_called=False) as m:
        robots = m.get("https://w.com/robots.txt")
        robots.side_effect = [httpx.Response(502, headers={"retry-after": "120"}), httpx.Response(404)]
        m.get("https://w.com/wp-json/wc/store/v1/products").mock(return_value=httpx.Response(200, json=[]))
        ingest.run(lake, [_woo_store()], WEEK, workers=2)
    assert _statuses(lake, "w.com") == ["error", "ok"] and robots.call_count == 2


def test_a_5xx_without_a_long_retry_after_is_still_retried_by_the_client(lake, clock):
    with respx.mock(assert_all_called=False) as m:
        m.get("https://w.com/robots.txt").mock(return_value=httpx.Response(404))
        route = m.get("https://w.com/wp-json/wc/store/v1/products")
        route.side_effect = [
            httpx.Response(502, headers={"retry-after": "5"}),
            httpx.Response(200, json=WOO_OK),
        ]
        out = ingest.run(lake, [_woo_store()], WEEK, workers=2)
    assert _statuses(lake, "w.com") == ["ok"] and route.call_count == 2 and not out["deferred"]


SQ_ITEM = {"id": "a1", "title": "Blend", "urlId": "blend", "variants": [{"price": 1800}]}


@pytest.mark.parametrize("status", [429, 503])
def test_a_squarespace_sitemap_that_always_blocks_falls_back_to_the_shop_paths_after_its_retries(
    lake, monkeypatch, clock, status
):
    """The sitemap's block defers the store (never read without it while a retry is left); on the last round the
    common shop paths are read, so its catalog isn't lost every week."""
    with respx.mock(assert_all_called=False) as m:
        m.get("https://q.com/robots.txt").mock(return_value=httpx.Response(404))
        sitemap = m.get("https://q.com/sitemap.xml").mock(
            return_value=httpx.Response(status, headers={"retry-after": WAIT})
        )
        shop = m.get("https://q.com/shop").mock(return_value=httpx.Response(200, json={"items": [SQ_ITEM]}))
        crawl_one(lake, monkeypatch, "squarespace", "q.com", "squarespace")
    assert fetches(lake, "squarespace", "q.com") == ["error", "error", "ok"]
    assert fetches(lake, "squarespace", "q.com", "products")[-1] == 1
    assert sitemap.call_count >= ingest.DEFERRED_ROUNDS + 1  # asked in every round
    assert shop.call_count == 1  # only on the last try


def test_a_squarespace_fallback_without_products_is_an_error_not_an_empty_catalog(lake, monkeypatch, clock):
    with respx.mock(assert_all_called=False) as m:
        m.get("https://q.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://q.com/sitemap.xml").mock(
            return_value=httpx.Response(429, headers={"retry-after": WAIT})
        )
        m.get(url__startswith="https://q.com/").mock(return_value=httpx.Response(404))
        crawl_one(lake, monkeypatch, "squarespace", "q.com", "squarespace")
    assert fetches(lake, "squarespace", "q.com") == ["error"] * (ingest.DEFERRED_ROUNDS + 1)
