"""The probe's requests and rows are what they were before its home and API steps moved into discover._home and
discover._api (docs/design/roaster-screen.md, section 2.1). The expected sequences below were recorded from
probe_one before the refactor, on the commit before it: the screen reuses the pieces, and the probe must not change.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from coffeedata import discover
from coffeedata.http import USER_AGENT, PoliteClient

SHOPIFY_HOME = "<html><script src='https://cdn.shopify.com/s/x.js'></script></html>"
WIX_HOME = "<html><img src='https://static.wixstatic.com/a.png'></html>"
ROBOTS_OK = httpx.Response(200, text="User-agent: *\nAllow: /\n")
P = "/products.json?limit=1"
W = "/wp-json/wc/store/v1/products?per_page=1"


def _run(routes: dict[str, httpx.Response | Exception], domain: str = "a.com") -> tuple[dict, list[str]]:
    """probe_one over a fake web: `routes` maps host + path (with query) to its answer; anything else is a 404."""
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == USER_AGENT
        key = request.url.host + request.url.raw_path.decode()
        asked.append(key)
        got = routes.get(key, httpx.Response(404))
        if isinstance(got, Exception):
            raise got
        return got

    with respx.mock as m:
        m.route().mock(side_effect=answer)
        with PoliteClient(min_interval=0, retries=1) as c:
            row = discover.probe_one(c, domain)
    return row, asked


CASES = {
    "shopify": (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=SHOPIFY_HOME),
         "a.com" + P: httpx.Response(200, json={"products": [{"id": 1}]})},
        {"status": "ok", "final_host": "a.com", "platform": "shopify", "http_status": 200},
        ["a.com/robots.txt", "a.com/", "a.com" + P],
    ),
    "woocommerce asks Shopify first, marker or not": (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text="<html>woocommerce</html>"),
         "a.com" + W: httpx.Response(200, json=[{"id": 1}])},
        {"status": "ok", "final_host": "a.com", "platform": "woocommerce", "http_status": 200},
        ["a.com/robots.txt", "a.com/", "a.com" + P, "a.com" + W],
    ),
    "another platform's marker still costs both API guesses": (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text=WIX_HOME)},
        {"status": "ok", "final_host": "a.com", "platform": "wix?", "http_status": 200},
        ["a.com/robots.txt", "a.com/", "a.com" + P, "a.com" + W],
    ),
    "no marker": (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(200, text="<html>hello</html>")},
        {"status": "ok", "final_host": "a.com", "platform": "other", "http_status": 200},
        ["a.com/robots.txt", "a.com/", "a.com" + P, "a.com" + W],
    ),
    "robots.txt disallows us": (
        {"a.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n")},
        {"status": "blocked", "final_host": "a.com", "platform": "", "http_status": None},
        ["a.com/robots.txt"],
    ),
    "a challenge on robots.txt": (
        {"a.com/robots.txt": httpx.Response(202, headers={"sg-captcha": "challenge"})},
        {"status": "blocked", "final_host": "a.com", "platform": "", "http_status": 202},
        ["a.com/robots.txt"],
    ),
    "robots.txt answers 503 on both hosts": (
        {"a.com/robots.txt": httpx.Response(503), "www.a.com/robots.txt": httpx.Response(503)},
        {"status": "robots_unavailable", "final_host": "", "platform": "", "http_status": None},
        ["a.com/robots.txt", "www.a.com/robots.txt"],
    ),
    "the apex is down, www answers": (
        {"a.com/robots.txt": httpx.ConnectError("down"), "www.a.com/robots.txt": ROBOTS_OK,
         "www.a.com/": httpx.Response(200, text=SHOPIFY_HOME),
         "www.a.com" + P: httpx.Response(200, json={"products": []})},
        {"status": "ok", "final_host": "www.a.com", "platform": "shopify", "http_status": 200},
        ["a.com/robots.txt", "www.a.com/robots.txt", "www.a.com/", "www.a.com" + P],
    ),
    "the home page redirects to www": (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(301, headers={"location": "https://www.a.com/"}),
         "www.a.com/robots.txt": ROBOTS_OK, "www.a.com/": httpx.Response(200, text=SHOPIFY_HOME),
         "www.a.com" + P: httpx.Response(200, json={"products": []})},
        {"status": "ok", "final_host": "www.a.com", "platform": "shopify", "http_status": 200},
        ["a.com/robots.txt", "a.com/", "www.a.com/robots.txt", "www.a.com/", "www.a.com" + P],
    ),
    "a dead site": (
        {"a.com/robots.txt": httpx.ConnectError("down"), "www.a.com/robots.txt": httpx.ConnectError("down")},
        {"status": "unreachable", "final_host": "", "platform": "", "http_status": None},
        ["a.com/robots.txt", "www.a.com/robots.txt"],
    ),
    "the home page is gone": (
        {"a.com/robots.txt": ROBOTS_OK, "a.com/": httpx.Response(410)},
        {"status": "http_410", "final_host": "a.com", "platform": "", "http_status": 410},
        ["a.com/robots.txt", "a.com/"],
    ),
    "the API paths are disallowed": (
        {"a.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /products.json\nDisallow: /wp-json/\n"),
         "a.com/": httpx.Response(200, text=SHOPIFY_HOME)},
        {"status": "ok", "final_host": "a.com", "platform": "shopify?", "http_status": 200},
        ["a.com/robots.txt", "a.com/"],
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", CASES)
def test_the_probe_asks_and_answers_as_before(name):
    routes, row, asked = CASES[name]
    assert _run(routes) == (row, asked)
