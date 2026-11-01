"""Closed, deactivated and password-protected stores get their own fetch status, `unavailable`: not an error and not
a block. Seen 2026-10-31: Big Guns (deactivated; its robots.txt disallows the whole site)."""

import httpx
import pytest
import respx

from coffeedata import http, ingest
from coffeedata.models import Roaster
from tests.test_ratelimit import FAST, fake_time

CLOSED_PAGE = "<html><body><h1>This store is unavailable</h1><p>Are you the store owner?</p></body></html>"


def _r(host: str) -> Roaster:
    return Roaster(roaster_id=host, name="R", host=host, platform="shopify")


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    with respx.mock(assert_all_called=False) as m:
        yield m


def _statuses(lake) -> dict[str, tuple[str, str]]:
    q = f"select roaster_id, status, error from {lake.scan('fetches')}"
    return {rid: (st, err or "") for rid, st, err in lake.connect().execute(q).fetchall()}


def test_closed_password_and_disallow_all_stores_are_unavailable(lake, mock, monkeypatch):
    fake_time(monkeypatch)
    for h in ("closed.com", "locked.com", "open.com", "nofeed.com"):
        robots = "User-agent: *\nDisallow: /products.json\n" if h == "nofeed.com" else ""
        mock.get(f"https://{h}/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    mock.get("https://gone.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    # robots.txt refused (403, e.g. an edge challenge): a refusal, retried later, never a closed store
    mock.get("https://challenged.com/robots.txt").mock(
        return_value=httpx.Response(403, text="Just a moment...")
    )
    mock.get("https://closed.com/products.json").mock(
        return_value=httpx.Response(402, headers={"content-type": "text/html"}, text=CLOSED_PAGE)
    )
    mock.get("https://locked.com/products.json").mock(
        return_value=httpx.Response(302, headers={"location": "https://locked.com/password"})
    )
    mock.get("https://locked.com/password").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text="<form>Enter store using password"
        )
    )
    mock.get("https://open.com/products.json").mock(return_value=httpx.Response(200, json={"products": []}))
    hosts = ("closed.com", "locked.com", "gone.com", "open.com", "nofeed.com", "challenged.com")
    out = ingest.run(lake, [_r(h) for h in hosts], "2026-11-02", workers=1, shopify_rps=1000, client_kw=FAST)

    got = _statuses(lake)
    assert {h: got[h][0] for h in hosts} == {
        "closed.com": "unavailable",  # Shopify's closed-store page
        "locked.com": "unavailable",  # a redirect to /password
        "gone.com": "unavailable",  # robots.txt disallows the whole site (Big Guns)
        "open.com": "ok",
        "nofeed.com": "error",  # disallows only the feed: a robots block, not a closed store
        "challenged.com": "error",
    }
    assert "password-protected" in got["locked.com"][1] and "unavailable" in got["closed.com"][1]
    assert out["deferred"].get("round_1") is None  # never retried in a deferred round


def test_an_unavailable_store_is_done_for_the_run(lake, mock, monkeypatch):
    """A resume or retry pass doesn't ask a closed store again this week."""
    fake_time(monkeypatch)
    mock.get("https://closed.com/robots.txt").mock(return_value=httpx.Response(404))
    mock.get("https://closed.com/products.json").mock(
        return_value=httpx.Response(404, headers={"content-type": "text/html"}, text=CLOSED_PAGE)
    )
    ingest.run(lake, [_r("closed.com")], "2026-11-02", workers=1, shopify_rps=1000, client_kw=FAST)
    monkeypatch.setattr(
        ingest.registry,
        "current",
        lambda lake: [ingest.registry.RoasterState(_r("closed.com"), "active", None)],
    )
    todo, _ = ingest.select_states(lake, "2026-11-02")
    assert todo == []
    todo, _ = ingest.select_states(lake, "2026-11-02", retry_failed=True)
    assert todo == []


def test_unavailable_reason_reads_only_what_the_store_says():
    req = httpx.Request("GET", "https://x.com/products.json")
    ok = httpx.Response(200, json={"products": []}, request=req)
    assert http.unavailable_reason(ok) is None
    html = httpx.Response(404, headers={"content-type": "text/html"}, text="Not found", request=req)
    assert http.unavailable_reason(html) is None  # a plain 404 is not a closed store
    closed = httpx.Response(402, headers={"content-type": "text/html"}, text=CLOSED_PAGE, request=req)
    assert "unavailable" in http.unavailable_reason(closed)
    sorry = "<p>Sorry, this shop is currently unavailable.</p>"
    gone = httpx.Response(404, headers={"content-type": "text/html"}, text=sorry, request=req)
    assert "unavailable" in http.unavailable_reason(gone)
