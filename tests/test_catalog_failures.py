"""A miss must never be stored as a successful (empty or partial) catalog: PR #9 review MF-1 / MF-2."""

import httpx
import pytest
import respx

from coffeedata import ingest
from coffeedata.adapters import generic, squarespace
from coffeedata.http import FetchError, PoliteClient
from coffeedata.models import Roaster

SQ = Roaster(roaster_id="sq.com", name="SQ", host="sq.com", platform="squarespace?")
GEN = Roaster(roaster_id="gen.com", name="GEN", host="gen.com", platform="other")
PATHS = squarespace.COMMON_SHOP_PATHS
ITEM = {
    "id": "i1",
    "title": "Ethiopia Guji",
    "fullUrl": "/shop/p/guji",
    "productType": 1,
    "variants": [
        {"id": "v1", "attributes": {"size": "12oz"}, "priceMoney": {"value": "20.00"}, "unlimited": True}
    ],
}


def collection(items):
    return httpx.Response(200, json={"items": items, "pagination": None})


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get("https://sq.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://gen.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://sq.com/sitemap.xml").mock(return_value=httpx.Response(404))
        yield m


def sq_paths(mock, responses: dict[str, httpx.Response], default: httpx.Response):
    for p in PATHS:
        mock.get(f"https://sq.com{p}").mock(return_value=responses.get(p, default))


def fetch_sq():
    with PoliteClient(min_interval=0, retries=1) as c:
        return squarespace.fetch(c, SQ)


def test_S1_every_path_403_raises(mock):
    sq_paths(mock, {}, httpx.Response(403))
    with pytest.raises(FetchError):
        fetch_sq()


def test_S2_404_then_json_collection(mock):
    sq_paths(mock, {"/store": collection([ITEM])}, httpx.Response(404))
    assert [i.title for i in fetch_sq()] == ["Ethiopia Guji"]


def test_S3_empty_collections_are_an_empty_catalog(mock):
    sq_paths(mock, {}, collection([]))
    assert fetch_sq() == []


def test_S4_later_page_failure_raises(mock):
    first = httpx.Response(
        200, json={"items": [ITEM], "pagination": {"nextPage": True, "nextPageOffset": 99}}
    )
    route = mock.get("https://sq.com/shop")
    route.side_effect = [first, httpx.Response(403)]
    with pytest.raises(FetchError):
        fetch_sq()


def test_S5_html_then_json_collection(mock):
    sq_paths(
        mock,
        {"/shop": httpx.Response(200, text="<html>shop</html>"), "/store": collection([ITEM])},
        httpx.Response(404),
    )
    assert len(fetch_sq()) == 1


def test_S6_every_path_404_is_no_shop(mock):
    sq_paths(mock, {}, httpx.Response(404))
    assert fetch_sq() == []


def test_squarespace_400_raises(mock):  # S7
    sq_paths(mock, {}, httpx.Response(400))
    with pytest.raises(FetchError):
        fetch_sq()


def test_S8_challenge_html_everywhere_raises(mock):
    sq_paths(mock, {}, httpx.Response(200, text="<html>Checking your browser… challenge</html>"))
    with pytest.raises(FetchError):
        fetch_sq()


def test_squarespace_robots_disallow_raises():
    with respx.mock(assert_all_called=False) as m:
        m.get("https://sq.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        )
        route = m.get(url__regex=r"https://sq.com/.+").mock(return_value=collection([ITEM]))
        with pytest.raises(FetchError):
            fetch_sq()
        assert route.call_count == 0


# --- generic -------------------------------------------------------------------------------------


def sitemap(n):
    return httpx.Response(
        200,
        text="<urlset>"
        + "".join(f"<url><loc>https://gen.com/products/p{i}</loc></url>" for i in range(n))
        + "</urlset>",
    )


def page(name):
    return httpx.Response(
        200,
        text=f'<script type="application/ld+json">{{"@type": "Product", "name": "{name} 12oz", '
        f'"sku": "{name}", "offers": {{"price": "20"}}}}</script>',
    )


def fetch_gen():
    with PoliteClient(min_interval=0, retries=1) as c:
        return generic.fetch(c, GEN)


def test_generic_one_failed_page_raises(mock):  # G1
    mock.get("https://gen.com/sitemap.xml").mock(return_value=sitemap(3))
    mock.get("https://gen.com/products/p0").mock(return_value=page("A"))
    mock.get("https://gen.com/products/p1").mock(return_value=httpx.Response(500))
    mock.get("https://gen.com/products/p2").mock(return_value=page("C"))
    with pytest.raises(FetchError):
        fetch_gen()


def test_G2_all_pages_fail_raises(mock):
    mock.get("https://gen.com/sitemap.xml").mock(return_value=sitemap(3))
    mock.get(url__regex=r"https://gen.com/products/p\d+").mock(return_value=httpx.Response(500))
    with pytest.raises(FetchError):
        fetch_gen()


def test_G3_pages_without_markup_are_an_empty_catalog(mock):
    mock.get("https://gen.com/sitemap.xml").mock(return_value=sitemap(12))
    route = mock.get(url__regex=r"https://gen.com/products/p\d+").mock(
        return_value=httpx.Response(200, text="<p>hi</p>")
    )
    assert fetch_gen() == []
    assert route.call_count == generic.GIVE_UP_AFTER


def test_generic_blocked_sitemap_raises():
    with respx.mock(assert_all_called=False) as m:
        m.get("https://gen.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        )
        with pytest.raises(FetchError):
            fetch_gen()


def test_generic_sitemap_error_raises(mock):
    mock.get("https://gen.com/sitemap.xml").mock(return_value=httpx.Response(503))
    with pytest.raises(FetchError):
        fetch_gen()


def test_generic_without_sitemap_is_empty(mock):
    mock.get("https://gen.com/sitemap.xml").mock(return_value=httpx.Response(404))
    assert fetch_gen() == []


def test_squarespace_403_week_is_an_error_not_an_empty_catalog(lake, mock):
    """End to end, the reviewer's MF-1 reproduction: the fetch row is an error and no snapshot is written."""
    sq_paths(mock, {}, httpx.Response(403))
    ingest.run(lake, [SQ], "2026-10-26", workers=1)
    con = lake.connect()
    assert con.execute(f"select status from {lake.scan('fetches')}").fetchall() == [("error",)]
    assert con.execute(f"select count(*) from {lake.scan('snapshots')}").fetchone()[0] == 0


def test_generic_404_page_is_a_delisted_product(mock):  # G4: a stale sitemap entry isn't a failed fetch
    mock.get("https://gen.com/sitemap.xml").mock(return_value=sitemap(3))
    mock.get("https://gen.com/products/p0").mock(return_value=page("A"))
    mock.get("https://gen.com/products/p1").mock(return_value=httpx.Response(404))
    mock.get("https://gen.com/products/p2").mock(return_value=page("C"))
    assert sorted(i.title for i in fetch_gen()) == ["A 12oz", "C 12oz"]
