"""The roaster screen's job (src/coffeedata/screen.py; docs/design/roaster-screen.md, sections 2 and 3).

Every site here is made up and served by a fake transport: no request leaves the test. The cases are the ones the
design names: a robots.txt refusal, a challenge, the cap of six requests, the run's deadline, the re-screen windows,
and the request order per platform.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta

import httpx
import pyarrow.parquet as pq
import pytest
import respx

from coffeedata import cli, control, quality, raw_contracts, screen, screen_rules
from coffeedata.http import USER_AGENT, DeadlineReached
from coffeedata.lake import SCHEMAS, Lake

ROBOTS_OK = httpx.Response(200, text="User-agent: *\nAllow: /\n")
SHOPIFY_HOME = (
    "<html><head><title>Made Up Roasters &amp; Co</title><script src='https://cdn.shopify.com/s/x.js'></script>"
    "<style>p{color:red}</style></head><body><p>We roast in house.</p></body></html>"
)
WOO_HOME = "<html><head><title>Woo Beans</title></head><body class='woocommerce'>Fresh beans</body></html>"
WIX_HOME = "<html><img src='https://static.wixstatic.com/a.png'>A cafe</html>"
PLAIN_HOME = "<html><title>Plain</title>hello</html>"
P250 = "/products.json?limit=250"
W25 = "/wp-json/wc/store/v1/products?per_page=25"
W5 = "/wp-json/wc/store/v1/products?per_page=5"


def shopify_product(i: int, **kw) -> dict:
    return {
        "id": i, "handle": f"coffee-{i}", "title": f"Ethiopia Lot {i}", "vendor": "Made Up Roasters",
        "product_type": "Coffee", "tags": ["single origin", "washed"], "body_html": "<p>Floral.</p>" + "x" * 3000,
        "variants": [{"id": i * 10, "title": "12 oz", "price": "19.00", "available": True, "grams": 340}],
    } | kw  # fmt: skip


def woo_product(i: int) -> dict:
    return {"id": i, "slug": f"bag-{i}", "name": f"House Blend {i}", "permalink": f"https://w.com/p/bag-{i}",
            "description": "<p>Chocolate.</p>", "prices": {"price": "1800", "currency_code": "USD",
                                                             "currency_minor_unit": 2},
            "tags": [{"name": "blend"}], "categories": [{"name": "Coffee"}], "is_in_stock": True}  # fmt: skip


class Web:
    """A fake web: host + path (with query) -> a response, an exception, or a callable. Anything else is a 404."""

    def __init__(self, routes: dict):
        self.routes, self.asked, self.agents = routes, [], set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = request.url.host + request.url.raw_path.decode()
        self.asked.append(key)
        self.agents.add(request.headers["user-agent"])
        got = self.routes.get(key, httpx.Response(404))
        if callable(got):
            got = got(request)
        if isinstance(got, Exception):
            raise got
        return got


def fast_client(**kw) -> screen.ScreenClient:
    return screen.client(**({"min_interval": 0, "retries": 1} | kw))


def one(routes: dict, domain: str = "a.com", **kw):
    web = Web(routes)
    with respx.mock as m:
        m.route().mock(side_effect=web)
        with fast_client(**kw) as c:
            got = screen.screen_one(c, domain)
    assert web.agents <= {USER_AGENT}  # the truthful user agent, on every request
    return got, web.asked


def shopify_site(n: int = 3, host: str = "a.com") -> dict:
    return {f"{host}/robots.txt": ROBOTS_OK, f"{host}/": httpx.Response(200, text=SHOPIFY_HOME),
            f"{host}{P250}": httpx.Response(200, json={"products": [shopify_product(i) for i in range(1, n + 1)]},
                                           headers={"set-cookie": "cart_currency=USD; path=/"})}  # fmt: skip


def made_up_web() -> dict:
    """Four invented places for a local build (never a test's own web): a roaster under its own name, a café that
    resells a registry roaster, a site whose robots.txt refuses us, and a platform the screen doesn't read."""
    roaster = [
        shopify_product(i, title=f"{o} Single Origin Coffee", body_html=f"<p>Origin: {o}. Roast: Light.</p>")
        for i, o in enumerate(["Ethiopia", "Colombia", "Kenya", "Peru", "Guatemala", "Rwanda"], 1)
    ]
    resold = [
        shopify_product(i, vendor="Counter Culture Coffee", title=f"Counter Culture Blend {i} Coffee")
        for i in range(1, 7)
    ]
    cafe_home = "<html><script src='https://cdn.shopify.com/x.js'></script>Proudly serving Counter Culture Coffee.</html>"
    return (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=SHOPIFY_HOME),
         "a.com" + P250: httpx.Response(200, json={"products": roaster})}
        | {"cafe.com/robots.txt": ROBOTS_OK, "cafe.com/": httpx.Response(200, text=cafe_home),
           "cafe.com" + P250: httpx.Response(200, json={"products": resold})}
        | {"b.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n")}
        | {"c.com/robots.txt": ROBOTS_OK, "c.com/": httpx.Response(200, text=WIX_HOME)}
    )  # fmt: skip


# --- the request order, per platform (section 3.1) ------------------------------------------------------------------


def test_a_shopify_store_costs_three_requests_and_its_first_page_is_recorded():
    (row, products), asked = one(shopify_site())
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + P250]
    assert row | {"duration_s": 0} == {
        "status": "ok", "refusal": None, "edge": None, "http_status": 200, "final_host": "a.com",
        "platform": "shopify", "page_size": 250, "products_read": 3, "page_full": False, "page_timeout": False,
        "home_title": "Made Up Roasters & Co", "home_text": "Made Up Roasters & Co We roast in house.",
        "requests": 3, "bytes": row["bytes"], "crawl_delay_s": None, "duration_s": 0,
    }  # fmt: skip
    assert row["bytes"] > len(SHOPIFY_HOME)
    p = products[0]
    assert (p["product_id"], p["handle"], p["title"], p["vendor"], p["product_type"]) == (
        "1", "coffee-1", "Ethiopia Lot 1", "Made Up Roasters", "Coffee")  # fmt: skip
    assert p["tags"] == ["single origin", "washed"]
    assert p["variants"] == [
        {"title": "12 oz", "grams": 340, "price": 19.0, "currency": "USD", "available": True}
    ]
    assert len(p["body_text"]) == screen.BODY_TEXT_CHARS and p["body_hash"]


def test_a_full_first_page_says_there_may_be_more():
    routes = shopify_site()
    routes["a.com" + P250] = httpx.Response(200, json={"products": [shopify_product(i) for i in range(250)]})
    (row, products), _ = one(routes)
    assert (row["products_read"], row["page_full"], len(products)) == (250, True, 250)


def test_a_woocommerce_marker_asks_the_store_api_first_and_never_shopify():
    (row, products), asked = one({
        "a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=WOO_HOME),
        "a.com" + W25: httpx.Response(200, json=[woo_product(1), woo_product(2)]),
    })  # fmt: skip
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + W25]
    assert (row["platform"], row["page_size"], row["products_read"], row["page_full"]) == (
        "woocommerce",
        25,
        2,
        False,
    )
    assert [p["title"] for p in products] == ["House Blend 1", "House Blend 2"]
    assert products[0]["tags"] == ["Coffee", "blend"]


def test_another_platforms_marker_costs_two_requests_and_no_api_guess():
    (row, products), asked = one(
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=WIX_HOME)}
    )
    assert asked == ["a.com/robots.txt", "a.com/"]
    assert (row["status"], row["platform"], row["page_size"], products) == ("ok", "wix?", None, [])
    assert row["home_text"] == "A cafe"


def test_no_marker_asks_shopify_then_woocommerce():
    (row, _), asked = one({"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=PLAIN_HOME)})
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + P250, "a.com" + W25]
    assert (row["status"], row["platform"], row["requests"]) == ("ok", "other", 4)


def test_home_text_is_capped():
    routes = shopify_site()
    routes["a.com/"] = httpx.Response(200, text="<html>cdn.shopify.com " + "word " * 20_000 + "</html>")
    (row, _), _ = one(routes)
    assert len(row["home_text"]) == screen.HOME_TEXT_CHARS


# --- refusals: recorded, and nothing else is fetched (section 3.4) --------------------------------------------------


def test_robots_txt_disallowing_us_ends_the_screen_after_one_request():
    (row, products), asked = one(
        {"a.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n")}
    )
    assert asked == ["a.com/robots.txt"]
    assert (row["status"], row["refusal"], row["edge"], row["requests"], products) == (
        "blocked", "robots_disallow", None, 1, [])  # fmt: skip


def test_robots_txt_naming_our_bot_is_honoured():
    robots = "User-agent: RegistryCoffeeBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    (row, _), asked = one({"a.com/robots.txt": httpx.Response(200, text=robots)})
    assert (asked, row["refusal"]) == (["a.com/robots.txt"], "robots_disallow")


@pytest.mark.parametrize("status", [401, 403])
def test_robots_txt_answering_401_or_403_disallows_everything(status):
    (row, _), asked = one({"a.com/robots.txt": httpx.Response(status)})
    assert asked == ["a.com/robots.txt"]
    assert (row["status"], row["refusal"], row["edge"]) == ("blocked", "robots_401_403", None)


def test_no_robots_txt_means_no_rules():
    routes = shopify_site()
    routes["a.com/robots.txt"] = httpx.Response(404)
    (row, products), asked = one(routes)
    assert (row["status"], len(products), len(asked)) == ("ok", 3, 3)


def test_a_robots_txt_that_fails_means_nothing_is_fetched():
    (row, _), asked = one(
        {"a.com/robots.txt": httpx.Response(503), "www.a.com/robots.txt": httpx.Response(503)}
    )
    assert asked == ["a.com/robots.txt", "www.a.com/robots.txt"]  # never the home page
    assert (row["status"], row["refusal"]) == ("robots_unavailable", None)


def test_a_robots_rule_against_the_product_path_is_a_refusal_and_the_path_is_never_asked():
    routes = shopify_site()
    routes["a.com/robots.txt"] = httpx.Response(200, text="User-agent: *\nDisallow: /products.json\n")
    (row, products), asked = one(routes)
    assert asked == ["a.com/robots.txt", "a.com/"]
    assert (row["status"], row["refusal"], row["platform"], products) == (
        "blocked",
        "robots_disallow",
        "shopify?",
        [],
    )
    assert row["home_text"]  # the home page it was allowed to read is kept


@pytest.mark.parametrize(
    ("response", "edge"),
    [
        (httpx.Response(202, headers={"sg-captcha": "challenge"}), "siteground"),
        (httpx.Response(403, headers={"cf-mitigated": "challenge", "server": "cloudflare"}), "cloudflare"),
        (httpx.Response(202), "other"),
    ],
)
def test_a_challenge_on_robots_txt_is_recorded_with_its_edge_and_never_worked_around(response, edge):
    (row, products), asked = one({"a.com/robots.txt": response})
    assert asked == ["a.com/robots.txt"]  # no retry, no www, no home page
    assert (row["status"], row["refusal"], row["edge"], products) == ("blocked", "challenge", edge, [])


def test_a_challenge_on_the_home_page_is_a_refusal():
    (row, _), asked = one({"a.com/robots.txt": ROBOTS_OK,
                           "a.com/": httpx.Response(403, headers={"cf-mitigated": "challenge"})})  # fmt: skip
    assert asked == ["a.com/robots.txt", "a.com/"]
    assert (row["status"], row["refusal"], row["edge"], row["home_text"]) == (
        "blocked",
        "challenge",
        "cloudflare",
        None,
    )


def test_a_challenge_on_the_product_page_is_a_refusal():
    routes = shopify_site()
    routes["a.com" + P250] = httpx.Response(202, headers={"sg-captcha": "challenge"})
    (row, products), asked = one(routes)
    assert len(asked) == 3
    assert (row["status"], row["refusal"], row["edge"], products) == (
        "blocked",
        "challenge",
        "siteground",
        [],
    )


def test_a_plain_403_on_the_home_page_is_its_status_with_no_refusal_word():
    (row, _), _ = one({"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(403)})
    assert (row["status"], row["refusal"], row["edge"], row["http_status"]) == ("http_403", None, None, 403)


def test_a_429_is_recorded_as_what_the_site_said():
    routes = {f"{h}/robots.txt": ROBOTS_OK for h in ("a.com", "www.a.com")}
    routes |= {"a.com/": httpx.Response(429), "www.a.com/": httpx.Response(429)}
    (row, _), _ = one(routes)
    assert (row["status"], row["refusal"]) == ("http_429", "http_429")


@pytest.mark.parametrize(
    "answer, status, refusal, edge",
    [
        (httpx.Response(429), "http_429", "http_429", None),
        (httpx.Response(430), "http_430", "http_430", None),
        (httpx.Response(503, headers={"sg-captcha": "challenge"}), "blocked", "challenge", "siteground"),
        (httpx.Response(503, headers={"cf-mitigated": "challenge"}), "blocked", "challenge", "cloudflare"),
        (httpx.Response(403, headers={"cf-mitigated": "challenge"}), "blocked", "challenge", "cloudflare"),
        (httpx.Response(202, headers={"sg-captcha": "challenge"}), "blocked", "challenge", "siteground"),
        (httpx.Response(503, headers={"retry-after": "120"}), "pause_503", None, None),
    ],
    ids=[
        "429",
        "430",
        "503-siteground",
        "503-cloudflare",
        "403-cloudflare",
        "202-siteground",
        "503-retry-after",
    ],
)
def test_a_refusal_on_the_catalog_api_stops_the_screen_and_is_recorded(answer, status, refusal, edge):
    # the review's case: the home page answers and names no platform, then /products.json refuses or pauses us.
    # Nothing more is asked of the host (never the Store API), and the row says what the site said.
    routes = {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=PLAIN_HOME), "a.com" + P250: answer,
              "a.com" + W25: httpx.Response(200, json=[woo_product(1)])}  # fmt: skip
    (row, products), asked = one(routes)
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + P250]
    assert (row["status"], row["refusal"], row["edge"], products) == (status, refusal, edge, [])
    b = screen.Breaker()
    b.note(row)
    # a known edge's challenge is reported, not counted; a 5xx pause is neither
    assert b.counted == (0 if edge == "siteground" or status == "pause_503" else 1)
    assert b.by_edge == ({edge: 1} if edge else {})


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_a_plain_5xx_on_the_catalog_api_is_not_a_refusal(code):
    # as the probe (#255): a server's trouble isn't an answer to us, so the next API is still asked; with nothing read,
    # the row says what the server said, and the place is asked again after 28 days
    routes = {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=PLAIN_HOME),
              "a.com" + P250: httpx.Response(code)}  # fmt: skip
    (row, products), asked = one(routes | {"a.com" + W25: httpx.Response(200, json=[woo_product(1)])})
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + P250, "a.com" + W25]
    assert (row["status"], row["platform"], row["refusal"], len(products)) == ("ok", "woocommerce", None, 1)
    (row, products), _ = one(routes)  # the Store API answers 404: nothing read
    assert (row["status"], row["refusal"], row["http_status"], products) == (f"http_{code}", None, code, [])
    assert screen_rules.transient(row["status"], row["refusal"])


def test_a_plain_5xx_on_the_home_page_still_asks_www_and_is_never_a_dead_site():
    routes = {f"{h}/robots.txt": ROBOTS_OK for h in ("a.com", "www.a.com")}
    (row, _), asked = one(
        routes | {"a.com/": httpx.Response(500), "www.a.com/": httpx.Response(200, text=PLAIN_HOME)}
    )
    assert asked[:4] == ["a.com/robots.txt", "a.com/", "www.a.com/robots.txt", "www.a.com/"]
    assert (row["status"], row["final_host"]) == ("ok", "www.a.com")
    (row, _), _ = one(routes | {"a.com/": httpx.Response(502), "www.a.com/": httpx.Response(502)})
    # transient, not dropped
    assert (row["status"], row["refusal"], row["http_status"]) == ("http_502", None, 502)
    assert screen_rules.transient(row["status"], row["refusal"])


def test_a_5xx_asking_us_to_wait_on_the_home_page_is_a_pause():
    routes = {f"{h}/robots.txt": ROBOTS_OK for h in ("a.com", "www.a.com")}
    routes |= {"a.com/": httpx.Response(503, headers={"retry-after": "120"}),
               "www.a.com/": httpx.Response(200, text=PLAIN_HOME)}  # fmt: skip
    (row, _), asked = one(routes)
    assert asked == ["a.com/robots.txt", "a.com/"]  # never www after it
    assert (row["status"], row["http_status"]) == ("pause_503", 503)
    assert screen_rules.transient(row["status"], row["refusal"])  # asked again after 28 days


def test_a_pause_on_the_home_page_is_never_asked_again_at_www():
    routes = {f"{h}/robots.txt": ROBOTS_OK for h in ("a.com", "www.a.com")}
    routes |= {"a.com/": httpx.Response(430), "www.a.com/": httpx.Response(200, text=SHOPIFY_HOME)}
    (row, _), asked = one(routes)
    assert asked == ["a.com/robots.txt", "a.com/"]
    assert (row["status"], row["refusal"], row["http_status"]) == ("http_430", "http_430", 430)


def test_a_robots_rule_against_a_guessed_api_path_is_not_a_refusal_of_the_site():
    # no marker on the home page: /products.json is a guess, so a rule against it says nothing about the site, and
    # the screen goes on to the Store API. With nothing read, the place is unread (not screened), never refused.
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /products.json\n")
    routes = {"a.com/robots.txt": robots, "a.com/": httpx.Response(200, text=PLAIN_HOME)}
    (row, products), asked = one(routes)
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + W25]
    assert (row["status"], row["refusal"], row["platform"], products) == ("ok", None, "other", [])
    # the same rule when the home page names Shopify: the site's catalog is refused (365 days)
    (row, _), _ = one(routes | {"a.com/": httpx.Response(200, text=SHOPIFY_HOME)})
    assert (row["status"], row["refusal"]) == ("blocked", "robots_disallow")


def test_a_dead_site_is_unreachable():
    down = httpx.ConnectError("down")
    (row, _), asked = one({"a.com/robots.txt": down, "www.a.com/robots.txt": down})
    assert (row["status"], row["refusal"], row["requests"]) == ("unreachable", None, 2)


def test_a_crawl_delay_is_recorded():
    routes = shopify_site()
    routes["a.com/robots.txt"] = httpx.Response(200, text="User-agent: *\nCrawl-delay: 0.01\n")
    (row, _), _ = one(routes)
    assert row["crawl_delay_s"] == 0.01


# --- the cap, timeouts and the deadline -----------------------------------------------------------------------------


def test_a_screen_never_sends_a_seventh_request():
    # a chain of redirects, each to a new host with its own robots.txt: the cap stops it, whatever it has read
    routes: dict = {}
    for i in range(10):
        routes[f"h{i}.com/robots.txt"] = ROBOTS_OK
        routes[f"h{i}.com/"] = httpx.Response(301, headers={"location": f"https://h{i + 1}.com/"})
    (row, products), asked = one(routes, domain="h0.com")
    assert len(asked) == screen.MAX_REQUESTS == 6
    # no answer about the site, not a dead one: not screened, and asked again after the transient wait
    assert (row["requests"], row["status"], products) == (6, "budget_spent", [])
    assert screen_rules.transient(row["status"], row["refusal"])


def test_the_cap_counts_retries_and_keeps_what_was_read(monkeypatch):
    # www redirect (4 requests), then a product page that keeps answering 500: one try, one retry, and no more
    routes = {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(301, headers={"location": "https://www.a.com/"}),
              "www.a.com/robots.txt": ROBOTS_OK, "www.a.com/": httpx.Response(200, text=PLAIN_HOME),
              "www.a.com" + P250: httpx.Response(500)}  # fmt: skip
    monkeypatch.setattr(
        "coffeedata.http.jittered", lambda s: 0
    )  # the client's own backoff, not waited out here
    (row, products), asked = one(routes, retries=3)
    web = Web({})
    web.asked = asked
    assert len(web.asked) == 6 and web.asked[-2:] == ["www.a.com" + P250] * 2
    assert (row["status"], row["final_host"], row["home_title"], row["requests"]) == (
        "ok",
        "www.a.com",
        "Plain",
        6,
    )


def test_a_woocommerce_page_that_times_out_steps_down_once_then_gives_up():
    timeout = httpx.ReadTimeout("slow")
    base = {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=WOO_HOME)}
    (row, products), asked = one(
        base | {"a.com" + W25: timeout, "a.com" + W5: httpx.Response(200, json=[woo_product(1)])}
    )
    assert asked == ["a.com/robots.txt", "a.com/", "a.com" + W25, "a.com" + W5]
    assert (row["platform"], row["page_size"], row["products_read"], row["page_timeout"]) == (
        "woocommerce",
        5,
        1,
        False,
    )
    (row, products), asked = one(base | {"a.com" + W25: timeout, "a.com" + W5: timeout})
    assert asked[-2:] == ["a.com" + W25, "a.com" + W5]
    assert (row["status"], row["platform"], row["page_timeout"], products) == ("ok", "woocommerce?", True, [])


def test_a_screen_cut_off_by_the_runs_deadline_leaves_no_row():
    def late(request):
        raise DeadlineReached("the run's deadline passed")

    got, _ = one({"a.com/robots.txt": ROBOTS_OK, "a.com/": late})
    assert got is None


def test_an_odd_site_is_its_own_row_never_the_runs_error():
    (row, products), _ = one(
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": lambda r: (_ for _ in ()).throw(RuntimeError("odd"))}
    )
    # our own failure is no answer about the site: transient, never `unreachable` (a dead site, dropped for 180 days)
    assert (row["status"], products) == ("error", [])
    assert screen_rules.transient(row["status"], row["refusal"])


def test_a_home_page_that_times_out_is_not_a_dead_site():
    slow = httpx.ReadTimeout("slow")
    routes = {f"{h}/robots.txt": ROBOTS_OK for h in ("a.com", "www.a.com")}
    routes |= {"a.com/": slow, "www.a.com/": slow}
    (row, _), _ = one(routes)
    assert (row["status"], row["refusal"]) == ("timeout", None)
    assert screen_rules.transient(row["status"], row["refusal"])
    at = datetime(2026, 12, 2, 7, tzinfo=UTC)
    assert screen_rules.next_due(at, row["status"], row["refusal"]) == date(2026, 12, 30)  # 28 days, not 180
    home = screen_rules.home_signals("", "a.com", screen_rules.KnownRoasters([]))
    assert screen_rules.tier(screen_rules.features(row, [], home))[0] == "not_screened"  # never dropped


def test_a_product_that_does_not_parse_is_left_out_not_the_screen():
    routes = shopify_site()
    routes["a.com" + P250] = httpx.Response(200, json={"products": [shopify_product(1), {"title": "no id"}]})
    (row, products), _ = one(routes)
    assert (row["products_read"], len(products)) == (2, 1)


# --- re-screen windows (section 3.5) --------------------------------------------------------------------------------

AT = datetime(2026, 12, 2, 7, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "refusal", "tries", "days"),
    [
        ("ok", None, 1, 180),  # suggested, review or dropped with no owner action
        ("unreachable", None, 1, 180),  # a dead site
        ("http_404", None, 1, 180),
        ("http_403", None, 1, 180),
        ("blocked", "robots_disallow", 1, 365),
        ("blocked", "robots_401_403", 1, 365),
        ("blocked", "challenge", 1, 365),
        ("robots_unavailable", None, 1, 28),
        ("robots_unavailable", None, 2, 180),  # two tries, then the long window
        ("http_429", "http_429", 1, 28),
        ("robots_unavailable", "http_429", 1, 28),
        ("http_503", None, 1, 28),
        ("http_503", None, 3, 180),
    ],
)
def test_next_due_holds_the_design_table(status, refusal, tries, days):
    for tier in (None, "suggested", "review", "dropped", "not_screened"):
        assert screen_rules.next_due(AT, status, refusal, tier, tries) == AT.date() + timedelta(days=days)


def test_last_screens_counts_transient_screens_in_a_row():
    d = lambda n: AT + timedelta(days=n)  # noqa: E731
    rows = [("a.com", d(0), "http_503", None), ("a.com", d(28), "robots_unavailable", None),
            ("b.com", d(0), "http_503", None), ("b.com", d(28), "ok", None),
            ("c.com", d(0), "ok", None), ("c.com", d(180), "http_503", None)]  # fmt: skip
    last = screen.last_screens(rows)
    assert {k: (v["status"], v["tries"]) for k, v in last.items()} == {
        "a.com": ("robots_unavailable", 2), "b.com": ("ok", 1), "c.com": ("http_503", 1)}  # fmt: skip


# --- selection (section 2.3) ----------------------------------------------------------------------------------------


def _candidate(rid: str, state: str = "MI") -> dict:
    return {"added_at": datetime(2026, 11, 1, tzinfo=UTC), "source": "overture-2026-10-21.0", "roaster_id": rid,
            "name": rid, "city": "", "state": state, "website": f"https://{rid}/", "locations": 1,
            "confidence": 0.9, "tags": ["cafe"], "roast_in_name": False}  # fmt: skip


def _screen_row(rid: str, at: datetime, status: str = "ok", refusal: str | None = None) -> dict:
    return {"run_date": at.date(), "batch_id": "b", "screen_id": f"{rid}-{at:%j}", "screened_at": at,
            "roaster_id": rid, "state": "MI", "status": status, "refusal": refusal}  # fmt: skip


@pytest.fixture
def seed(tmp_path):
    path = tmp_path / "roasters.csv"
    path.write_text(
        "roaster_id,name,city,state,locations,opted_out,final_host,platform,last_probed,sources,country,alias_of\n"
        "inregistry.com,In Registry,,MI,1,false,inregistry.com,shopify,2026-10-26,curated,US,\n"
        "optedout.com,Opted Out,,MI,1,true,,,,curated,US,\n"
    )
    return path


def test_select_takes_only_places_that_are_due(lake, seed):
    places = ["new1.com", "new2.com", "other-state.com", "inregistry.com", "optedout.com", "flagged-roaster.com",
              "flagged-not.com", "facebook.com", "fresh.com", "stale.com", "refused.com", "retry.com"]  # fmt: skip
    lake.write("candidates", "candidates/source=overture/x.parquet",
               [_candidate(p, "OH" if p == "other-state.com" else "MI") for p in places])  # fmt: skip
    day = date(2026, 12, 2)
    ago = lambda n: datetime(2026, 12, 2, 7, tzinfo=UTC) - timedelta(days=n)  # noqa: E731
    lake.write_partition("screens", "2026-11-01", "b1", [
        _screen_row("fresh.com", ago(30)),  # answered 30 days ago: not due for 180
        _screen_row("stale.com", ago(181)),
        _screen_row("refused.com", ago(200), "blocked", "robots_disallow"),  # the site said no: 365
        _screen_row("retry.com", ago(29), "robots_unavailable"),  # transient: 28
    ])  # fmt: skip
    doc = {"sources": ["flagged-roaster.com"], "not_roasters": ["flagged-not.com"]}
    got = screen.select(lake, "mi", day.isoformat(), doc=doc, seed=seed)
    assert set(got) == {"new1.com", "new2.com", "stale.com", "retry.com"}
    # never-screened places first, then the re-screens; each group in the salted order, the same on every call
    assert set(got[:2]) == {"new1.com", "new2.com"}
    assert got[:2] == sorted(got[:2], key=screen.order_key) and got[2:] == sorted(
        got[2:], key=screen.order_key
    )
    assert screen.select(lake, "MI", day.isoformat(), doc=doc, seed=seed) == got
    assert screen.select(lake, "MI", day.isoformat(), cap=1, doc=doc, seed=seed) == got[:1]


# --- the run: raw, append-only, private -----------------------------------------------------------------------------


def _files(lake: Lake, table: str) -> dict[str, bytes]:
    return {
        str(p.relative_to(lake.root)): p.read_bytes()
        for p in sorted((lake.root / table).glob("run_date=2*/*.parquet"))
    }


def _rows(lake: Lake, table: str) -> list[dict]:
    return [
        r
        for p in sorted((lake.root / table).glob("run_date=2*/*.parquet"))
        for r in pq.read_table(p).to_pylist()
    ]


def _run(lake: Lake, routes: dict, todo: list[str], run_date: str = "2026-12-02", **kw) -> tuple[dict, Web]:
    web = Web(routes)
    with respx.mock as m:
        m.route().mock(side_effect=web)
        out = screen.run(lake, todo, "MI", run_date, workers=2, make_client=fast_client, **kw)
    return out, web


def test_a_run_writes_exactly_the_contracts_columns(lake):
    routes = shopify_site(2) | {"b.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n")}
    out, _ = _run(lake, routes, ["a.com", "b.com"])
    assert (out["screened"], out["products"], out["left"], out["stopped"], out["files"]) == (2, 2, 0, None, 1)
    assert out["by_status"] == {"ok": 1, "blocked": 1}
    screens, products = _rows(lake, "screens"), _rows(lake, "screen_products")
    assert list(screens[0]) == SCHEMAS["screens"].names == raw_contracts.columns("screen", "screens")
    assert (
        list(products[0])
        == SCHEMAS["screen_products"].names
        == raw_contracts.columns("screen", "screen_products")
    )
    by = {r["roaster_id"]: r for r in screens}
    assert by["a.com"]["state"] == "MI" and by["a.com"]["run_date"] == date(2026, 12, 2)
    assert {p["screen_id"] for p in products} == {by["a.com"]["screen_id"]}
    # nothing derived is stored: no tier, no score, no coffee count
    assert not {"tier", "score", "coffees", "admitted", "reason"} & set(screens[0])
    # the screen adds nothing to the registry or the probes
    assert not list((lake.root / "registry").glob("source=screen*")) and not _rows(lake, "probes")


def test_a_second_run_adds_files_and_rewrites_none(lake):
    _run(lake, shopify_site(2), ["a.com"])
    before = {t: _files(lake, t) for t in ("screens", "screen_products")}
    _run(lake, shopify_site(3), ["a.com"], run_date="2027-06-02")
    for t in ("screens", "screen_products"):
        after = _files(lake, t)
        assert before[t].items() <= after.items() and len(after) == len(before[t]) + 1
    screens = _rows(lake, "screens")
    assert len(screens) == 2 and len({r["screen_id"] for r in screens}) == 2  # a re-screen is a new row
    assert len(_rows(lake, "screen_products")) == 5


def test_screen_tables_are_never_released():
    released = set(quality.RELEASE_TABLES) | set(quality.OPS_TABLES)
    assert not {"screens", "screen_products", "stg_screens", "stg_screen_products", "screen_verdict_batches",
                "screen_verdicts", "stg_screen_verdicts"} & released  # fmt: skip


def test_no_place_is_started_after_the_deadline(lake):
    out, web = _run(lake, shopify_site(), ["a.com", "b.com"], deadline_s=0)
    assert (out["screened"], out["left"], web.asked, _rows(lake, "screens")) == (0, 2, [], [])


def test_rows_are_written_when_the_run_fails(lake, monkeypatch):
    real = screen.screen_one

    def flaky(c, domain):
        if domain == "boom.com":
            raise KeyboardInterrupt
        return real(c, domain)

    monkeypatch.setattr(screen, "screen_one", flaky)
    web = Web(shopify_site(1))
    with respx.mock as m, pytest.raises(KeyboardInterrupt):
        m.route().mock(side_effect=web)
        screen.run(lake, ["a.com", "boom.com"], "MI", "2026-12-02", workers=1, make_client=fast_client)
    assert [r["roaster_id"] for r in _rows(lake, "screens")] == ["a.com"]


def test_a_flush_that_fails_halfway_never_writes_a_screen_twice(lake, monkeypatch):
    # the review's case: the screens are written, then the screen_products write fails once. The flush after writes
    # only the products; before, the whole batch went back and its screens were written again under a new batch,
    # which failed three unique tests and stopped the release.
    real, failed = lake.write_partition, []

    def flaky(table, run_date, batch_id, rows):
        if table == "screen_products" and not failed:
            failed.append(batch_id)
            raise OSError("disk")
        return real(table, run_date, batch_id, rows)

    monkeypatch.setattr(lake, "write_partition", flaky)
    with pytest.raises(OSError):
        _run(
            lake, shopify_site(2), ["a.com"], batch_size=1
        )  # the run stops on the error, flushing what it has
    screens, products = _rows(lake, "screens"), _rows(lake, "screen_products")
    assert failed and len(screens) == len({r["screen_id"] for r in screens}) == 1
    assert {p["screen_id"] for p in products} == {screens[0]["screen_id"]} and len(products) == 2


# --- the circuit breaker (section 3.3) ------------------------------------------------------------------------------


def _note(b: screen.Breaker, n: int, **row) -> None:
    for _ in range(n):
        b.note({"status": "ok", "refusal": None, "edge": None} | row)


def test_the_breaker_counts_refusals_but_not_a_known_edges_challenges():
    b = screen.Breaker(share=0.08, min_places=100)
    _note(b, 60)
    _note(b, 30, status="blocked", refusal="challenge", edge="siteground")  # reported, not counted
    _note(b, 2, status="blocked", refusal="robots_disallow")  # the site's own rule: not a refusal of this run
    _note(b, 8, status="http_429", refusal="http_429")
    assert (b.places, b.counted, b.tripped) == (100, 8, False)  # exactly 8% doesn't trip
    assert b.report()["by_edge"] == {"siteground": 30}
    _note(b, 1, status="blocked", refusal="challenge", edge="cloudflare")
    assert (b.counted, b.tripped) == (9, True)
    assert b.report()["by_edge"] == {"siteground": 30, "cloudflare": 1}


@pytest.mark.parametrize(
    "row",
    [{"status": "http_403"}, {"status": "http_401"}, {"status": "blocked", "refusal": "robots_401_403"},
     {"status": "blocked", "refusal": "challenge", "edge": "other"}, {"status": "robots_unavailable", "refusal": "http_429"}],
)  # fmt: skip
def test_what_counts_toward_the_breaker(row):
    b = screen.Breaker(min_places=1)
    _note(b, 1, **row)
    assert b.counted == 1


def test_the_breaker_judges_no_share_on_too_few_places():
    b = screen.Breaker(min_places=100)
    _note(b, 50, status="http_429", refusal="http_429")
    assert not b.tripped


def test_a_tripped_breaker_stops_starting_places_and_keeps_what_finished(lake):
    refuse = {f"r{i}.com/robots.txt": httpx.Response(429) for i in range(4)}
    refuse |= {f"www.r{i}.com/robots.txt": httpx.Response(429) for i in range(4)}
    todo = [f"r{i}.com" for i in range(4)] + [f"later{i}.com" for i in range(20)]
    web = Web(refuse)
    with respx.mock as m:
        m.route().mock(side_effect=web)
        out = screen.run(lake, todo, "MI", "2026-12-02", workers=1, make_client=fast_client,
                         breaker=screen.Breaker(share=0.5, min_places=4))  # fmt: skip
    assert (out["stopped"], out["screened"], out["left"]) == ("refusals", 4, 20)
    assert not [a for a in web.asked if a.startswith(("later", "www.later"))]  # none was started
    assert len(_rows(lake, "screens")) == 4  # their rows are written
    assert out["breaker"]["by_refusal"] == {"http_429": 4}


# --- the command ----------------------------------------------------------------------------------------------------


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**({"state": "mi", "max_places": 3000, "workers": 2, "run_date": "2026-12-02",
                                  "deadline_min": 90.0, "dry_run": False} | kw))  # fmt: skip


def _jobs(lake: Lake) -> list[dict]:
    return [r for r in control.rows(lake) if r["job"] == "screen"]


def test_the_command_records_its_run(lake, monkeypatch, capsys):
    monkeypatch.setattr(screen, "select", lambda *a, **k: ["a.com"])
    real = screen.client
    monkeypatch.setattr(screen, "client", lambda **kw: real(**(kw | {"min_interval": 0, "retries": 1})))
    monkeypatch.setattr("coffeedata.registry.read_served", lambda lake: None)
    with respx.mock as m:
        m.route().mock(side_effect=Web(shopify_site(2)))
        cli.cmd_screen(_args())
    (end,) = _jobs(lake)  # control.rows: the attempt's latest row
    assert (end["status"], end["todo"], end["ok"], end["errors"], end["skipped"]) == ("done", 1, 1, 0, 0)
    detail = control.detail_of(end)
    assert (detail["state"], detail["screened"], detail["products"], detail["by_status"]) == (
        "MI",
        1,
        2,
        {"ok": 1},
    )
    assert "screened 1 of 1 places" in capsys.readouterr().out


def test_a_dry_run_requests_nothing(lake, monkeypatch, capsys):
    monkeypatch.setattr(screen, "select", lambda *a, **k: ["a.com", "b.com"])
    monkeypatch.setattr("coffeedata.registry.read_served", lambda lake: None)
    monkeypatch.setattr(screen, "run", lambda *a, **k: pytest.fail("a dry run screens nothing"))
    cli.cmd_screen(_args(dry_run=True))
    assert "2 places are due; nothing was requested" in capsys.readouterr().out
    assert _rows(lake, "screens") == []


def test_a_stopped_run_ends_stopped_and_exits_non_zero(lake, monkeypatch):
    monkeypatch.setattr(screen, "select", lambda *a, **k: ["a.com"])
    monkeypatch.setattr("coffeedata.registry.read_served", lambda lake: None)
    report = {
        "places": 200,
        "counted": 30,
        "share": 0.08,
        "by_edge": {"cloudflare": 30},
        "by_refusal": {"challenge": 30},
    }
    monkeypatch.setattr(screen, "run", lambda *a, **k: {"screened": 200, "products": 0, "by_status": {"blocked": 30, "ok": 170},
                                                         "files": 1, "left": 800, "stopped": "refusals", "breaker": report})  # fmt: skip
    with pytest.raises(SystemExit) as e:
        cli.cmd_screen(_args())
    assert "30 of 200 places refused us" in str(e.value) and "worked around" in str(e.value)
    end = _jobs(lake)[-1]
    assert (end["status"], control.detail_of(end)["stopped"], control.detail_of(end)["refusals"]) == (
        "stopped",
        "refusals",
        report,
    )


def test_the_screen_command_is_registered(lake, monkeypatch, capsys):
    monkeypatch.setattr(screen, "select", lambda lake, state, run_date, cap, doc: ["a.com"] * (cap == 3000))
    monkeypatch.setattr("coffeedata.registry.read_served", lambda lake: None)
    cli.main(["screen", "--state", "MI", "--dry-run"])
    assert "screen MI: 1 places are due" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["screen"])  # one state per run: there is no default
