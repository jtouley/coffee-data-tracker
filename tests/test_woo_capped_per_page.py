"""WooCommerce stores that cap `per_page`: the Store API's default page is 10 products, and a store can answer 10
whatever `per_page` asks for while its X-WP-Total counts more. Until this rule, the adapter stopped at that short first
page, so such a store was read one page deep every week (10 of 44 products, a partial fetch each time). Now the first
page's length is taken as the store's page size, and the rest is read at it, through the same client: per-host pacing,
robots.txt and the run's deadline apply to every page. Synthetic stores only (`*.example`)."""

from __future__ import annotations

import random
import time

import httpx
import pytest
import respx

from coffeedata import http, ingest
from coffeedata.adapters import woocommerce
from coffeedata.http import Blocked, PartialCatalog, PoliteClient
from coffeedata.models import Roaster

HOST = "capped.example"
BASE = f"https://{HOST}/wp-json/wc/store/v1/products"


def _r() -> Roaster:
    return Roaster(roaster_id=HOST, name="Capped", host=HOST, platform="woocommerce")


def _products(n: int) -> list[dict]:
    return [
        {"id": i, "name": f"Coffee {i}", "slug": f"c{i}", "prices": {"price": "1800"}}
        for i in range(1, n + 1)
    ]


class Store:
    """A Store API products route over `products`, `cap` to a page at most (None: honours `per_page`), with the
    headers a store sends: X-WP-Total (`total`, its own count, which may be wrong; None leaves it out) and
    X-WP-TotalPages (`pages`). `empty_from`: pages from this one on answer []. `ignores_page`: every page is page 1.
    `on_page`: called with each page number asked for, before it answers. Records each (per_page, page) asked."""

    def __init__(self, products, *, cap=None, total="count", pages=None, empty_from=None, ignores_page=False,
                 on_page=None):  # fmt: skip
        self.products, self.cap, self.empty_from, self.ignores_page, self.on_page = (
            products, cap, empty_from, ignores_page, on_page)  # fmt: skip
        self.total = str(len(products)) if total == "count" else total
        self.pages = pages
        self.asked: list[tuple[int, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        asked = int(request.url.params.get("per_page", 10))
        page = int(request.url.params.get("page", 1))
        self.asked.append((asked, page))
        if self.on_page:
            self.on_page(page)
        size = min(asked, self.cap) if self.cap else asked
        page = 1 if self.ignores_page else page
        rows = self.products[(page - 1) * size : page * size]
        if self.empty_from is not None and page >= self.empty_from:
            rows = []
        headers = {}
        if self.total is not None:
            headers["X-WP-Total"] = self.total
        if self.pages is not None:
            headers["X-WP-TotalPages"] = self.pages
        return httpx.Response(200, json=rows, headers=headers)


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(f"https://{HOST}/robots.txt").mock(return_value=httpx.Response(404))
        yield m


def _serve(mock: respx.MockRouter, store: Store) -> Store:
    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=store)
    return store


def _ids(items) -> list[int]:
    return sorted(int(i.product_id) for i in items)


# --- a capped store is read whole ----------------------------------------------------------------------------------


def test_a_store_that_caps_per_page_at_10_is_read_whole(mock):
    """The case behind this rule: 10 products on the first page while X-WP-Total says 44."""
    store = _serve(mock, Store(_products(44), cap=10))
    with PoliteClient(min_interval=0) as c:
        items = woocommerce.fetch(c, _r())
    assert _ids(items) == list(range(1, 45))  # whole: no PartialCatalog
    # the first page as asked, then the store's own size, page by page; the short last page (4) ends it
    assert store.asked == [(100, 1), (10, 2), (10, 3), (10, 4), (10, 5)]


def test_x_wp_totalpages_alone_also_shows_the_cap(mock):
    store = _serve(mock, Store(_products(44), cap=10, total=None, pages="5"))
    with PoliteClient(min_interval=0) as c:
        assert _ids(woocommerce.fetch(c, _r())) == list(range(1, 45))
    assert store.asked == [(100, 1), (10, 2), (10, 3), (10, 4), (10, 5)]


def test_a_cap_that_isnt_10_is_read_at_its_own_size(mock):
    store = _serve(mock, Store(_products(130), cap=48))
    with PoliteClient(min_interval=0) as c:
        assert len(woocommerce.fetch(c, _r())) == 130
    assert store.asked == [(100, 1), (48, 2), (48, 3)]


def test_a_capped_stores_variations_are_read_in_bulk_at_its_size_too(mock):
    """The bulk variations query goes through the same paging: a capped store's 15 sizes come back 10 and 5, with no
    size asked for on its own."""
    parent = {
        "id": 1,
        "name": "Ethiopia",
        "slug": "ethiopia",
        "prices": {"price": "1800", "price_range": {"min_amount": "1800", "max_amount": "3200"}},
        "variations": [
            {"id": 100 + k, "attributes": [{"name": "Size", "value": f"{k} oz"}]} for k in range(1, 16)
        ],
    }
    sizes = [{"id": 100 + k, "parent": 1, "prices": {"price": str(1800 + k)}, "is_in_stock": True}
             for k in range(1, 16)]  # fmt: skip
    listing, bulk = Store([parent], cap=10), Store(sizes, cap=10)

    def route(request: httpx.Request) -> httpx.Response:
        return (bulk if request.url.params.get("type") == "variation" else listing)(request)

    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=route)
    one_by_one = mock.get(url__regex=rf"^{BASE}/\d+$").mock(return_value=httpx.Response(500))
    with PoliteClient(min_interval=0) as c:
        [item] = woocommerce.fetch(c, _r())
    assert len(item.variants) == 15 and all(v.price_usd for v in item.variants)
    assert bulk.asked == [(100, 1), (10, 2)] and not one_by_one.called


# --- a store that honours per_page is asked for nothing more --------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "asked"), [(44, [(100, 1)]), (100, [(100, 1), (100, 2)]), (150, [(100, 1), (100, 2)])]
)
def test_a_store_that_honours_per_page_gets_no_extra_request(mock, n, asked):
    store = _serve(mock, Store(_products(n), pages=str(-(-n // 100))))
    with PoliteClient(min_interval=0) as c:
        assert len(woocommerce.fetch(c, _r())) == n
    assert store.asked == asked  # as before this rule (100 products: page 2 is empty, as it always was)


def test_a_short_page_after_full_ones_isnt_a_cap(mock):
    """A store that honoured per_page on page 1 and then answers short, with a count above what was read, ends there
    as before: partial by X-WP-Total, never read again at the short page's size."""
    store = _serve(mock, Store(_products(141)[:116], total="141"))
    with (
        PoliteClient(min_interval=0) as c,
        pytest.raises(PartialCatalog, match="116 products read, X-WP-Total says 141"),
    ):
        woocommerce.fetch(c, _r())
    assert store.asked == [(100, 1), (100, 2)]


# --- a count that is wrong, or missing ------------------------------------------------------------------------------


def test_a_count_above_the_catalog_reads_it_and_stays_partial(mock):
    """X-WP-Total says 60, the store has 44 at 10 a page: every product read, at no request past the short last
    page, and still partial, because the store's count differs."""
    store = _serve(mock, Store(_products(44), cap=10, total="60"))
    with (
        PoliteClient(min_interval=0) as c,
        pytest.raises(PartialCatalog, match="44 products read, X-WP-Total says 60") as e,
    ):
        woocommerce.fetch(c, _r())
    assert len(e.value.items) == 44 and len(store.asked) == 5


@pytest.mark.parametrize(("total", "pages"), [("45", "1"), ("30", "5")])
def test_count_headers_that_disagree_ask_for_nothing_more(mock, total, pages):
    """Every count header present must say there is more. A store that honours per_page with 30 products, an
    X-WP-Total of 45 and an X-WP-TotalPages of 1; or an X-WP-Total the first page reaches with a stale X-WP-TotalPages
    of 5: no second request."""
    store = _serve(mock, Store(_products(30), total=total, pages=pages))
    with PoliteClient(min_interval=0) as c:
        try:
            assert len(woocommerce.fetch(c, _r())) == 30
        except PartialCatalog as e:
            assert "30 products read, X-WP-Total says 45" in str(e)
    assert store.asked == [(100, 1)]


def test_a_count_below_the_first_page_asks_for_nothing_more(mock):
    store = _serve(mock, Store(_products(44), cap=10, total="1"))
    with (
        PoliteClient(min_interval=0) as c,
        pytest.raises(PartialCatalog, match="10 products read, X-WP-Total says 1"),
    ):
        woocommerce.fetch(c, _r())
    assert store.asked == [(100, 1)]


@pytest.mark.parametrize("total", [None, "", "many"])
def test_no_readable_count_leaves_the_first_page_as_before(mock, total):
    """No X-WP-Total (or one that isn't a number) and no X-WP-TotalPages: nothing says there is more, so nothing more
    is asked for; the fetch is what it was before this rule."""
    store = _serve(mock, Store(_products(44), cap=10, total=total))
    with PoliteClient(min_interval=0) as c:
        assert len(woocommerce.fetch(c, _r())) == 10
    assert store.asked == [(100, 1)]


# --- an empty page, a repeated page, and no page cap ----------------------------------------------------------------


def test_an_empty_page_ends_the_paging_and_the_fetch_is_partial(mock):
    store = _serve(mock, Store(_products(44), cap=10, empty_from=3))
    with (
        PoliteClient(min_interval=0) as c,
        pytest.raises(PartialCatalog, match="20 products read, X-WP-Total says 44") as e,
    ):
        woocommerce.fetch(c, _r())
    assert len(e.value.items) == 20 and store.asked == [(100, 1), (10, 2), (10, 3)]


def test_an_empty_first_page_is_never_taken_as_a_cap(mock):
    store = _serve(mock, Store(_products(44), cap=10, empty_from=1))
    with PoliteClient(min_interval=0) as c, pytest.raises(PartialCatalog, match="0 products read"):
        woocommerce.fetch(c, _r())
    assert store.asked == [(100, 1)]


def test_a_capped_store_that_ignores_page_stops_at_the_repeat(mock):
    store = _serve(mock, Store(_products(44), cap=10, ignores_page=True))
    with PoliteClient(min_interval=0) as c, pytest.raises(PartialCatalog, match="10 products read"):
        woocommerce.fetch(c, _r())
    assert store.asked == [(100, 1), (10, 2)]


def test_no_page_cap_a_capped_store_is_read_past_the_old_max_pages(mock):
    """The owner removed MAX_PAGES (20) on 2026-11-02, and this rule adds none: 250 products at 10 a page are 25
    pages, read to the end. What bounds the paging is the store's count, an empty or repeated page, and the deadline."""
    assert not hasattr(woocommerce, "MAX_PAGES")
    store = _serve(mock, Store(_products(250), cap=10))
    with PoliteClient(min_interval=0) as c:
        assert len(woocommerce.fetch(c, _r())) == 250
    assert len(store.asked) == 26 and store.asked[-1] == (10, 26)  # 25 full pages and the empty 26th


def test_a_timeout_after_the_cap_asks_for_pages_that_still_divide_it(mock):
    """Read at the store's 10, a page that times out is asked for again at 5 (TIMEOUT_PER_PAGE's sizes that divide
    10), from the same offset: no product skipped or read twice."""
    store = Store(_products(44), cap=10)
    timed_out = []

    def route(request: httpx.Request) -> httpx.Response:
        if (
            request.url.params.get("per_page") == "10"
            and request.url.params.get("page") == "3"
            and not timed_out
        ):
            timed_out.append(1)
            raise httpx.ReadTimeout("slow", request=request)
        return store(request)

    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=route)
    with PoliteClient(min_interval=0, retries=1) as c:
        assert _ids(woocommerce.fetch(c, _r())) == list(range(1, 45))
    assert store.asked[:3] == [(100, 1), (10, 2), (5, 5)]  # offset 20 at 5 a page is page 5


def _timing_out(store: Store, slow) -> callable:
    """`store`, except that a request for which `slow(per_page, page)` is true times out."""

    def route(request: httpx.Request) -> httpx.Response:
        if slow(int(request.url.params["per_page"]), int(request.url.params["page"])):
            store.asked.append((int(request.url.params["per_page"]), int(request.url.params["page"])))
            raise httpx.ReadTimeout("slow", request=request)
        return store(request)

    return route


def test_a_timeout_at_a_cap_no_timeout_size_divides_falls_back_to_a_divisor_of_the_offset(mock):
    """Capped at 48, 130 products, page 2 times out once: asked again 24 a page (the largest size up to 25 that
    divides the 48 read), and read whole."""
    store = Store(_products(130), cap=48)
    once: list[int] = []

    def slow(per_page: int, page: int) -> bool:
        if (per_page, page) == (48, 2) and not once:
            once.append(1)
            return True
        return False

    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=_timing_out(store, slow))
    with PoliteClient(min_interval=0, retries=1) as c:
        assert _ids(woocommerce.fetch(c, _r())) == list(range(1, 131))
    assert store.asked == [(100, 1), (48, 2), (24, 3), (24, 4), (24, 5), (24, 6)]


def test_a_capped_store_that_times_out_at_every_size_is_stored_partial_with_what_was_read(mock):
    """Never an error with nothing stored: the 48 products of page 1 are a partial fetch, with the reason, and it
    isn't counted as a deadline stop."""
    store = Store(_products(130), cap=48)
    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(
        side_effect=_timing_out(store, lambda per_page, page: page > 1)
    )
    with (
        PoliteClient(min_interval=0, retries=1) as c,
        pytest.raises(PartialCatalog, match="timed out at page") as e,
    ):
        woocommerce.fetch(c, _r())
    assert _ids(e.value.items) == list(range(1, 49))
    # its cause is the timeout, not the clock (respx chains a raised exception to itself, so the chain is looked at
    # one step, not walked as ingest.stopped_by_deadline does)
    assert isinstance(e.value.__cause__, woocommerce.PagingTimedOut) and "deadline" not in str(e.value)
    assert store.asked == [(100, 1), (48, 2), (24, 3), (4, 13)]  # 48, 24 and 4 a page, all from offset 48


def test_a_timeout_before_any_cap_still_fails_as_before(mock):
    store = Store(_products(130))
    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=_timing_out(store, lambda per_page, page: True))
    with PoliteClient(min_interval=0, retries=1) as c, pytest.raises(http.FetchError) as e:
        woocommerce.fetch(c, _r())
    assert not isinstance(e.value, PartialCatalog)
    assert store.asked == [(100, 1), (25, 1), (5, 1)]


def test_a_capped_store_that_answers_random_samples_is_bounded_by_its_own_count(mock):
    """A store that ignores `page` and answers a different random 10 each time (orderby=rand behind a cache): never
    a whole repeated page, so only the store's own count bounds it, 200 products plus one page: 21 requests at most.
    Its products are each kept once."""
    products = _products(200)
    rng = random.Random(262)
    asked: list[tuple[int, int]] = []

    def route(request: httpx.Request) -> httpx.Response:
        asked.append((int(request.url.params["per_page"]), int(request.url.params["page"])))
        return httpx.Response(200, json=rng.sample(products, 10), headers={"X-WP-Total": "200"})

    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=route)
    with PoliteClient(min_interval=0) as c:
        try:
            items = woocommerce.fetch(c, _r())
        except PartialCatalog as e:
            items = e.items
    assert 2 <= len(asked) <= 21
    ids = [i.product_id for i in items]
    assert len(ids) == len(set(ids))


def test_a_page_that_overlaps_an_earlier_one_keeps_each_product_once(mock):
    """A catalog shifting during the read: page 2 repeats 5 of page 1's products. Each product is one item."""
    products = _products(20)
    pages = {1: products[0:10], 2: products[5:15], 3: products[15:20]}

    def route(request: httpx.Request) -> httpx.Response:
        rows = pages.get(int(request.url.params["page"]), [])
        return httpx.Response(200, json=rows, headers={"X-WP-Total": "20"})

    mock.get(url__regex=rf"^{BASE}(\?.*)?$").mock(side_effect=route)
    with PoliteClient(min_interval=0) as c:
        assert _ids(woocommerce.fetch(c, _r())) == list(range(1, 21))


# --- the safeguards: the deadline, pacing, robots.txt ---------------------------------------------------------------


def test_the_deadline_mid_paging_is_a_deadline_stop(mock):
    """The run's deadline passes while page 3 is read: no page 4 is asked for, the 30 products read are stored as
    partial, and the partial is counted as the deadline's (#261: ingest.stopped_by_deadline), not a count mismatch."""
    client = PoliteClient(min_interval=0)

    def on_page(page: int) -> None:
        if page == 3:
            client.deadline = time.monotonic() - 1

    store = _serve(mock, Store(_products(44), cap=10, on_page=on_page))
    with client as c, pytest.raises(PartialCatalog) as e:
        woocommerce.fetch(c, _r())
    assert len(e.value.items) == 30 and store.asked == [(100, 1), (10, 2), (10, 3)]
    assert ingest.stopped_by_deadline(e.value) and "deadline" in str(e.value)


@pytest.fixture
def sleeps(monkeypatch):
    """Every sleep the client asks for, on a fake clock that each sleep moves on (no real sleeping)."""
    out: list[float] = []
    now = [1000.0]

    def sleep(s: float) -> None:
        out.append(s)
        now[0] += s

    monkeypatch.setattr(http.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(http.time, "sleep", sleep)
    return out


def test_every_page_at_the_stores_size_keeps_the_hosts_crawl_delay(sleeps):
    with respx.mock(assert_all_called=False) as m:
        m.get(f"https://{HOST}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 5\n")
        )
        store = _serve(m, Store(_products(44), cap=10))
        with PoliteClient(min_interval=1.0, retries=1) as c:
            assert len(woocommerce.fetch(c, _r())) == 44
    assert len(store.asked) == 5
    assert len(sleeps) == 5 and all(
        4.9 < s <= 5 for s in sleeps
    )  # one Crawl-delay before each of the 5 pages


def test_robots_that_disallow_the_store_api_still_stop_it_before_any_page():
    with respx.mock(assert_all_called=False) as m:
        m.get(f"https://{HOST}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /wp-json/\n")
        )
        store = _serve(m, Store(_products(44), cap=10))
        with PoliteClient(min_interval=0) as c, pytest.raises(Blocked):
            woocommerce.fetch(c, _r())
    assert store.asked == []
